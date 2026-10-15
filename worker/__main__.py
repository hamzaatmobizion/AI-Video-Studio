"""python -m worker serve | login | check-gemini | doctor"""
import argparse
import json
import logging
import shutil
import sys

from worker.config import get_settings


def _build():
    from worker.gemini_browser import GeminiBrowserGenerator
    from worker.gemini_session import GeminiSession, ProfileLock
    from worker.jobs import JobStore
    from worker.pipeline import Runner

    s = get_settings()
    s.ensure_dirs()
    store = JobStore(s.db_path)
    lock = ProfileLock(s.data / "profile.lock")
    runner = Runner(s, store, GeminiBrowserGenerator(s), lock)
    session = GeminiSession(s, store, lock)
    return s, store, runner, session


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
    app = create_app(s, store, runner, session)
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
    pause = store.kv_get("account_pause")
    if pause:
        ok = False
        print(f"✗ Generation paused: {pause['message']}")
    print("All good." if ok else "Fix the items marked ✗.")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m worker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the worker API + job runner")
    sub.add_parser("login", help="open the manual Gemini sign-in window")
    sub.add_parser("check-gemini", help="check the session and which UI selectors match")
    sub.add_parser("doctor", help="check configuration and tools")
    args = ap.parse_args()
    return {"serve": cmd_serve, "login": cmd_login, "check-gemini": cmd_check, "doctor": cmd_doctor}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
