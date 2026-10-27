"""python -m worker serve | login | check-gemini | check-flow | doctor"""
import argparse
import json
import logging
import shutil
import sys

from worker.config import get_settings


def _build():
    from worker.gemini_browser import GeminiBrowserGenerator
    from worker.gemini_session import GeminiSession, ProfileLock
    from worker.pipeline import Runner

    s, store = _store()
    lock = ProfileLock(s.data / "profile.lock")
    runner = Runner(s, store, GeminiBrowserGenerator(s), lock, flow=_flow_generator(s))
    session = GeminiSession(s, store, lock)
    return s, store, runner, session


def _store():
    from worker.jobs import JobStore

    s = get_settings()
    s.ensure_dirs()
    return s, JobStore(s.db_path)


def _flow_generator(s):
    from worker.flow_browser import FlowBrowserGenerator

    return FlowBrowserGenerator(s)


def _flow_session(s, store, lock, open_project: bool = False):
    """Google Flow's sign-in window and check: the same profile and lock as Gemini, its own status and pause."""
    from worker import flow_browser
    from worker.gemini_session import GeminiSession

    return GeminiSession(s, store, lock, inspector=lambda st: flow_browser.inspect(st, in_project=open_project),
                         opener=flow_browser.open_plain_window, status_key="flow_status",
                         pause_key="account_pause:flow", name="Google Flow")


def _fail_on_problems(s) -> None:
    probs = s.problems()
    if probs:
        print("Worker configuration problems:\n  - " + "\n  - ".join(probs), file=sys.stderr)
        sys.exit(2)


def cmd_serve(_args) -> int:
    import uvicorn

    from worker.api import create_app

    s = get_settings()
    _fail_on_problems(s)
    s, store, runner, session = _build()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app(s, store, runner, session, flow_session=_flow_session(s, store, runner.lock))
    logging.getLogger("worker").info("worker listening on %s:%s (profile %s)", s.API_HOST, s.API_PORT,
                                     runner.profile.get("name"))
    uvicorn.run(app, host=s.API_HOST, port=s.API_PORT, access_log=False, log_level="warning")
    return 0


def cmd_login(_args) -> int:
    s, store, runner, session = _build()
    try:
        session.open_window()
    except Exception as e:
        print(getattr(e, "message", str(e)))
        return 1
    input("A browser window opened. Sign in to Google, open Gemini once, then press Enter here… ")
    result = session.done()
    print(f"Gemini: {result.get('state')} — {result.get('message')}")
    return 0 if result.get("state") == "connected" else 1


def cmd_check(_args) -> int:
    s, store, runner, session = _build()
    try:
        result = session.check()
    except Exception as e:
        print(getattr(e, "message", str(e)))
        return 1
    print(f"Session: {result.get('state')} — {result.get('message')}")
    if result.get("model"):
        print(f"Model picker shows: {result['model']}")
    if result.get("plan"):
        print(f"Plan: {result['plan']}   Video tool: {result.get('video') or 'not checked'}")
    print("Selectors (key → first matching candidate):")
    for key, sel in (result.get("selectors") or {}).items():
        print(f"  {'✓' if sel else '✗'} {key:20s} {sel or ''}")
    if result.get("screenshot"):
        print(f"Screenshot: {result['screenshot']}")
    return 0 if result.get("state") == "connected" else 1


def cmd_check_flow(args) -> int:
    """Read-only by default: Flow's home page and the sign-in only. `--project` also opens an empty project to check
    the controls clips use (more activity on the account, so only when Flow's page has changed)."""
    s, store, runner, _ = _build()
    session = _flow_session(s, store, runner.lock, open_project=args.project)
    try:
        result = session.check()
    except Exception as e:
        print(getattr(e, "message", str(e)))
        return 1
    print(f"Google Flow: {result.get('state')} — {result.get('message')}")
    if result.get("model"):
        print(f"Model menu shows: {result['model']}  (each clip uses {runner.generators['flow'].profile['model_label']})")
    print("Selectors (key → first matching candidate; project keys only match with --project):")
    for key, sel in (result.get("selectors") or {}).items():
        print(f"  {'✓' if sel else '✗'} {key:20s} {sel or ''}")
    if result.get("screenshot"):
        print(f"Screenshot: {result['screenshot']}")
    return 0 if result.get("state") == "connected" else 1


def cmd_doctor(_args) -> int:
    s = get_settings()
    ok = True
    for p in s.problems():
        ok = False
        print(f"✗ {p}")
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool):
            print(f"✓ {tool} found")
        else:
            ok = False
            print(f"✗ {tool} not found (brew install ffmpeg / apt install ffmpeg)")
    try:
        from pathlib import Path

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            exe = Path(p.chromium.executable_path)
        if exe.exists() or s.BROWSER_CHANNEL:
            print("✓ Playwright browser installed")
        else:
            ok = False
            print("✗ Playwright Chromium missing: .venv/bin/playwright install chromium")
    except Exception as e:
        ok = False
        print(f"✗ Playwright not usable ({type(e).__name__}): pip install -r requirements-worker.txt")
    s.ensure_dirs()
    from worker.jobs import JobStore

    store = JobStore(s.db_path)
    gs = store.kv_get("gemini_status")
    print(f"• Gemini session: {gs['state'] if gs else 'not checked yet (run check-gemini)'}")
    fs = store.kv_get("flow_status")
    print(f"• Google Flow session: {fs['state'] if fs else 'not checked yet (run check-flow)'}")
    for key, name in (("account_pause", "Gemini"), ("account_pause:flow", "Google Flow")):
        pause = store.kv_get(key)
        if pause:
            ok = False
            print(f"✗ {name} generation paused: {pause['message']}")
    print("All good." if ok else "Fix the items marked ✗.")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m worker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the worker API + job runner")
    sub.add_parser("login", help="open the manual Gemini sign-in window")
    sub.add_parser("check-gemini", help="check the session and which UI selectors match")
    flow = sub.add_parser("check-flow", help="check the Google Flow session (read-only: its home page only)")
    flow.add_argument("--project", action="store_true",
                      help="also open an empty Flow project to check the clip controls (only after Flow changes)")
    sub.add_parser("doctor", help="check configuration and tools")
    args = ap.parse_args()
    return {"serve": cmd_serve, "login": cmd_login, "check-gemini": cmd_check, "check-flow": cmd_check_flow,
            "doctor": cmd_doctor}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
