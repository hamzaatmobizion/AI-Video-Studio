import logging
import os
import stat
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from worker.api import create_app
from worker.config import WorkerSettings
from worker.gemini_browser import StopText, install_navigation_guard, load_ui, stop_error
from worker.gemini_session import Busy, GeminiSession, ProfileLock
from worker.generator import (
    Cancelled,
    GenerationError,
    LimitReached,
    SafetyRefusal,
    SignedOut,
    VerificationRequired,
)
from worker.jobs import JobStore
from worker.pipeline import Runner
from tests.fakes import JPG, PNG, TestPatternGenerator

KEY = "k" * 32
AUTH = {"Authorization": f"Bearer {KEY}"}
SECRET_PROMPT = "a lighthouse keeper named Zorblax feeding seagulls"


@pytest.fixture
def ws(tmp_path):
    s = WorkerSettings(_env_file=None, STUDIO_DATA_PATH=tmp_path / "data", STUDIO_API_KEY=KEY, MAX_RETRIES=1)
    s.ensure_dirs()
    return s


@pytest.fixture
def store(ws):
    return JobStore(ws.db_path)


@pytest.fixture
def lock(ws):
    return ProfileLock(ws.data / "profile.lock")


def make_runner(ws, store, lock, script=None, **kw):
    return Runner(ws, store, TestPatternGenerator(script, **kw), lock)


class FakeProc:
    def __init__(self):
        self.alive = True

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.alive = False


def make_session(ws, store, lock, state="connected"):
    return GeminiSession(ws, store, lock, inspector=lambda s: {"state": state, "message": "ok", "model": "Veo",
                                                               "plan": "paid", "video": "available"},
                         opener=lambda s: FakeProc())


@pytest.fixture
def api(ws, store, lock):
    runner = make_runner(ws, store, lock)
    session = make_session(ws, store, lock)
    app = create_app(ws, store, runner, session, start_background=False)
    with TestClient(app) as c:
        c.runner = runner
        c.session = session
        yield c


# ---- config / security ------------------------------------------------------------
def test_refuses_non_loopback_and_weak_key(tmp_path):
    s = WorkerSettings(_env_file=None, API_HOST="0.0.0.0", STUDIO_API_KEY="short")
    probs = " ".join(s.problems())
    assert "loopback" in probs and "24" in probs
    assert WorkerSettings(_env_file=None, STUDIO_API_KEY=KEY).problems() == []


def test_empty_profile_path_uses_default(tmp_path):
    s = WorkerSettings(_env_file=None, STUDIO_DATA_PATH=tmp_path, BROWSER_PROFILE_PATH="")
    assert s.profile_dir == tmp_path / "browser-profile"


def test_data_dirs_owner_only(ws):
    assert stat.S_IMODE(os.stat(ws.data).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(ws.profile_dir).st_mode) == 0o700


def test_api_requires_key(api):
    assert api.get("/health").status_code == 401
    assert api.get("/health", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert api.get("/health", headers=AUTH).json()["ok"] is True


def test_body_size_limit(api):
    big = b"x" * (36 * 1024 * 1024)
    r = api.post("/videos", headers={**AUTH, "Content-Type": "application/json"}, content=big)
    assert r.status_code == 413


# ---- /videos contract ----------------------------------------------------------------
def test_create_json_and_status(api, store):
    r = api.post("/videos", headers=AUTH, json={"prompt": "boat", "aspect": "9:16", "ref": "gen-1"})
    jid = r.json()["job_id"]
    assert api.get(f"/videos/{jid}", headers=AUTH).json()["status"] == "queued"
    assert api.get("/videos/nope", headers=AUTH).status_code == 404


def test_validation(api):
    assert api.post("/videos", headers=AUTH, json={"prompt": "", "aspect": "9:16"}).status_code == 400
    assert api.post("/videos", headers=AUTH, json={"prompt": "x", "aspect": "4:3"}).status_code == 400
    r = api.post("/videos", headers=AUTH, data={"prompt": "x", "aspect": "9:16"},
                 files=[("images", ("a.png", b"GIF89a....", "image/png"))])
    assert r.status_code == 400


def test_multipart_images_saved(api, store):
    r = api.post("/videos", headers=AUTH, data={"prompt": "x", "aspect": "16:9"},
                 files=[("images", ("a.png", PNG, "image/png")), ("images", ("b.jpg", JPG, "image/jpeg"))])
    job = store.get(r.json()["job_id"])
    assert [p.rsplit(".", 1)[1] for p in job["images"]] == ["png", "jpg"]


def test_end_to_end_single_clip(api, store, ws):
    jid = api.post("/videos", headers=AUTH, json={"prompt": "boat", "aspect": "9:16"}).json()["job_id"]
    assert api.runner.tick() is True
    st = api.get(f"/videos/{jid}", headers=AUTH).json()
    assert st["status"] == "done" and st["video_url"] == f"/videos/{jid}/file"
    body = api.get(st["video_url"], headers=AUTH).content
    assert body[4:8] == b"ftyp"
    clip = ws.clips_dir / jid / "attempt-1.mp4"
    assert stat.S_IMODE(os.stat(clip).st_mode) == 0o444  # original is read-only
    dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                         "-of", "csv=p=0", str(clip)]))
    assert 7.5 <= dur <= 8.5  # one ~8 s clip


def test_cancel_queued_and_running(api, store, ws, lock):
    jid = api.post("/videos", headers=AUTH, json={"prompt": "a", "aspect": "9:16"}).json()["job_id"]
    api.delete(f"/videos/{jid}", headers=AUTH)
    st = api.get(f"/videos/{jid}", headers=AUTH).json()
    assert st == {**st, "status": "failed", "error": "Cancelled."}
    assert api.delete("/videos/nope", headers=AUTH).status_code == 404

    def cancelling(req, should_cancel):
        store.update(req.job_id, cancel_requested=1)
        if should_cancel():
            raise Cancelled()

    runner = make_runner(ws, store, lock, [cancelling])
    jid2 = store.create("b", "9:16", None)
    runner.tick()
    assert store.get(jid2)["status"] == "cancelled"


# ---- retries / stops -----------------------------------------------------------------
def test_qc_failure_retries_then_fails_and_keeps_originals(ws, store, lock):
    runner = make_runner(ws, store, lock, [("short", 3), ("short", 3)])
    jid = store.create("x", "9:16", None)
    runner.tick()
    assert store.get(jid)["status"] == "queued" and store.get(jid)["attempts"] == 1
    runner.tick()
    job = store.get(jid)
    assert job["status"] == "failed" and "Quality check failed" in job["error"] and job["attempts"] == 2
    assert (ws.clips_dir / jid / "attempt-1.mp4").exists() and (ws.clips_dir / jid / "attempt-2.mp4").exists()
    assert runner.tick() is False  # no loop


def test_generation_error_then_success(ws, store, lock):
    runner = make_runner(ws, store, lock, [GenerationError("Gemini reported an error."), "ok"])
    jid = store.create("x", "9:16", None)
    runner.tick()
    runner.tick()
    assert store.get(jid)["status"] == "done"
    assert (ws.clips_dir / jid / "attempt-1.mp4").exists()


def test_unknown_exception_is_bounded(ws, store, lock):
    runner = make_runner(ws, store, lock, [RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")])
    jid = store.create("x", "9:16", None)
    for _ in range(5):
        runner.tick()
    job = store.get(jid)
    assert job["status"] == "failed" and len(runner.generator.calls) == 2


def test_safety_refusal_never_retried(ws, store, lock):
    runner = make_runner(ws, store, lock, [stop_error("safety")])
    jid = store.create("x", "9:16", None)
    runner.tick()
    job = store.get(jid)
    assert job["status"] == "failed" and "safety" in job["error"]
    assert runner.tick() is False and len(runner.generator.calls) == 1
    assert runner.generator.calls[0].prompt == "x"  # never reworded


@pytest.mark.parametrize("exc,kind", [
    (stop_error("limit"), "limit_reached"),
    (stop_error("signed_out"), "authentication_required"),
    (stop_error("verification"), "manual_intervention_required"),
    (stop_error("no_subscription"), "subscription_required"),
])
def test_account_stop_pauses_everything(api, ws, store, lock, exc, kind):
    runner = make_runner(ws, store, lock, [exc])
    a = store.create("a", "9:16", None)
    b = store.create("b", "9:16", None)
    runner.tick()
    assert store.get(a)["status"] == "failed" and store.get(a)["attempts"] == 0  # doesn't count
    assert store.get(b)["status"] == "failed" and store.get(b)["error"] == exc.user_message
    assert runner.paused()["kind"] == kind
    assert runner.tick() is False and len(runner.generator.calls) == 1  # stopped, no retry loop
    r = api.post("/videos", headers=AUTH, json={"prompt": "c", "aspect": "9:16"})
    assert r.status_code == 409 and r.json()["detail"] == exc.user_message
    health = api.get("/health", headers=AUTH).json()
    assert health["account_paused"]["kind"] == kind
    api.post("/gemini/resume", headers=AUTH)
    assert runner.paused() is None
    assert api.post("/videos", headers=AUTH, json={"prompt": "c", "aspect": "9:16"}).status_code == 200


def test_pause_is_visible_before_the_job_reads_failed(ws, store, lock):
    """The web app keeps a video's finished scenes only if /health already shows the pause."""
    runner = make_runner(ws, store, lock, [stop_error("limit")])
    jid = store.create("a", "9:16", None)
    seen, update = [], store.update

    def spy(job_id, **kw):
        if job_id == jid and kw.get("status") == "failed":
            seen.append(runner.paused())
        return update(job_id, **kw)

    store.update = spy
    runner.tick()
    assert seen and seen[0]["kind"] == "limit_reached"


# ---- crash recovery -------------------------------------------------------------------
def test_crash_recovery(ws, store, lock):
    jid = store.create("x", "9:16", None)
    store.update(jid, status="running")
    store.recover()
    assert store.get(jid)["status"] == "queued" and store.get(jid)["interruptions"] == 1
    store.update(jid, status="running")
    store.recover()
    assert store.get(jid)["status"] == "failed"


def test_restart_continues_queued_job(ws, store, lock):
    jid = store.create("x", "9:16", None)
    store.update(jid, status="running")
    store2 = JobStore(ws.db_path)  # "restart"
    store2.recover()
    runner = make_runner(ws, store2, lock)
    runner.tick()
    assert store2.get(jid)["status"] == "done"


# ---- profile lock / sign-in -----------------------------------------------------------
def test_generation_waits_while_sign_in_window_open(ws, store, lock):
    session = make_session(ws, store, lock)
    runner = make_runner(ws, store, lock)
    jid = store.create("x", "9:16", None)
    session.open_window()
    assert session.window_open
    assert runner.tick() is False and store.get(jid)["status"] == "queued"
    with pytest.raises(Busy):
        session.check()
    result = session.done()
    assert result["state"] == "connected" and not session.window_open
    assert runner.tick() is True and store.get(jid)["status"] == "done"


def test_lock_is_cross_process(ws):
    a = ProfileLock(ws.data / "profile.lock")
    b = ProfileLock(ws.data / "profile.lock")
    assert a.try_acquire("generation")
    assert not b.try_acquire("check")
    assert b.held_by_other_process() == "generation"
    a.release("generation")
    assert b.try_acquire("check")
    b.release("check")


def test_status_reflects_pause(api, store):
    api.runner.pause("limit_reached", "Limit reached message")
    st = api.get("/gemini/status", headers=AUTH).json()
    assert st["state"] == "limit_reached" and st["message"] == "Limit reached message"


def test_gemini_login_endpoints(api):
    assert api.post("/gemini/login", headers=AUTH).json()["login_window_open"] is True
    assert api.post("/gemini/check", headers=AUTH).status_code == 409
    done = api.post("/gemini/login/done", headers=AUTH).json()
    assert done["login_window_open"] is False
    for _ in range(50):  # the check after closing runs in the background
        if not api.get("/gemini/status", headers=AUTH).json()["checking"]:
            break
        time.sleep(0.1)
    assert api.get("/gemini/status", headers=AUTH).json()["state"] == "connected"
    st = api.get("/gemini/status", headers=AUTH).json()
    assert st["model"] == "Veo" and st["plan"] == "paid" and st["video"] == "available"


# ---- Gemini UI config -----------------------------------------------------------------
def test_quitting_sign_in_window_checks_automatically(ws, store, lock):
    session = make_session(ws, store, lock)
    assert session.open_window()["login_window_open"] is True
    assert session.status()["state"] == "unknown"
    session._proc.alive = False  # the user quit the window with ⌘Q
    st = session.status()
    assert st["login_window_open"] is False
    for _ in range(50):
        if not session.checking:
            break
        time.sleep(0.1)
    st = session.status()
    assert st["state"] == "connected" and st["plan"] == "paid" and st["checking"] is False
    assert lock.try_acquire("next")  # the profile was released for generation


def test_check_button_runs_in_background(api):
    st = api.post("/gemini/check?background=true", headers=AUTH).json()
    assert st["checking"] is True  # answers at once; the page shows progress
    for _ in range(50):
        if not api.get("/gemini/status", headers=AUTH).json()["checking"]:
            break
        time.sleep(0.1)
    assert api.get("/gemini/status", headers=AUTH).json()["state"] == "connected"
    api.post("/gemini/login", headers=AUTH)
    assert api.post("/gemini/check?background=true", headers=AUTH).status_code == 409  # window still open


def test_check_interrupted_by_shutdown_says_so(ws, store, lock):
    def killed(_s):
        raise Exception("Page.goto: Connection closed while reading from the driver")

    session = GeminiSession(ws, store, lock, inspector=killed, opener=lambda s: FakeProc())
    assert "interrupted" in session.check()["message"]
    assert "(Exception)" not in session.status()["message"]


def test_waits_for_chrome_already_on_profile(ws, store, lock):
    import os

    from worker.gemini_browser import PROFILE_IN_USE, launch_context, profile_browser_pid
    from worker.generator import GenerationError

    ws.profile_dir.mkdir(parents=True, exist_ok=True)
    singleton = ws.profile_dir / "SingletonLock"
    os.symlink(f"some-host.local-{os.getpid()}", singleton)  # a live process holds the profile
    assert profile_browser_pid(ws) == os.getpid()

    class NoLaunch:
        class chromium:  # noqa: N801
            @staticmethod
            def launch_persistent_context(**kw):
                raise AssertionError("must not launch while the profile is in use")

    with pytest.raises(GenerationError, match="still open"):
        launch_context(NoLaunch, ws, wait=0)
    with pytest.raises(Busy, match="still open"):
        make_session(ws, store, lock).open_window()

    singleton.unlink()
    os.symlink("some-host.local-999999", singleton)  # stale lock from a crashed Chrome
    assert profile_browser_pid(ws) is None


def test_launch_retries_when_browser_closes_at_start(ws, monkeypatch):
    from worker import gemini_browser

    monkeypatch.setattr(gemini_browser.time, "sleep", lambda s: None)

    class TargetClosedError(Exception):
        pass

    calls = []

    class Flaky:
        class chromium:  # noqa: N801
            @staticmethod
            def launch_persistent_context(**kw):
                calls.append(kw)
                if len(calls) < 3:
                    raise TargetClosedError()
                return "ctx"

    assert gemini_browser.launch_context(Flaky, ws) == "ctx" and len(calls) == 3
    calls.clear()
    with pytest.raises(TargetClosedError):
        gemini_browser.launch_context(Flaky, ws, attempts=2)
    assert calls[0]["chromium_sandbox"] is True


def test_ui_config_complete():
    ui = load_ui()
    assert set(ui["allowed_hosts"]) == {"gemini.google.com", "accounts.google.com", "consent.google.com",
                                        "www.google.com"}
    for key in ("prompt_input", "send_button", "video_tool", "response", "response_video", "file_input",
                "sign_in_button", "captcha", "download_button", "model_switch", "model_option",
                "upload_consent_agree"):
        assert ui["selectors"][key], key
    for kind in ("limit", "verification", "safety", "error", "signed_out", "no_subscription"):
        assert ui["patterns"][kind], kind


@pytest.mark.parametrize("text,kind", [
    ("You've reached your daily video generation limit. Your limit resets tomorrow.", "limit"),
    ("Our systems have detected unusual traffic from your computer network.", "verification"),
    ("Please verify it's you", "verification"),
    ("Upgrade to Google AI Pro to create videos with Veo", "no_subscription"),
    ("I can't create that video because it goes against our policies.", "safety"),
    ("Something went wrong. Please try again.", "error"),
    ("Here's your video of a paper boat drifting at dusk.", None),
    ("Generating your video. This could take a few minutes.", None),
])
def test_stop_text_classification(text, kind):
    assert StopText(load_ui()).classify(text) == kind


def test_stop_errors_are_typed():
    assert isinstance(stop_error("limit"), LimitReached)
    assert isinstance(stop_error("signed_out"), SignedOut)
    assert isinstance(stop_error("verification"), VerificationRequired)
    assert isinstance(stop_error("safety"), SafetyRefusal)


def test_navigation_guard_blocks_other_hosts():
    class Frame:
        parent_frame = None

    class Req:
        def __init__(self, url, nav=True, top=True):
            self.url, self._nav = url, nav
            self.frame = Frame() if top else type("F", (), {"parent_frame": object()})()

        def is_navigation_request(self):
            return self._nav

    class Route:
        def __init__(self):
            self.result = None

        def abort(self):
            self.result = "abort"

        def continue_(self):
            self.result = "continue"

    class Ctx:
        def route(self, pattern, handler):
            self.handler = handler

    ctx = Ctx()
    install_navigation_guard(ctx, load_ui()["allowed_hosts"])

    def run(req):
        r = Route()
        ctx.handler(r, req)
        return r.result

    assert run(Req("https://gemini.google.com/app")) == "continue"
    assert run(Req("https://accounts.google.com/signin")) == "continue"
    assert run(Req("https://evil.example.com/")) == "abort"
    assert run(Req("https://evil.example.com/x.js", nav=False)) == "continue"  # subresource
    assert run(Req("https://ads.example.com/", top=False)) == "continue"  # iframe, not top-level


# ---- logging hygiene -------------------------------------------------------------------
def test_logs_never_contain_prompts(api, caplog):
    caplog.set_level(logging.DEBUG)
    api.post("/videos", headers=AUTH, json={"prompt": SECRET_PROMPT, "aspect": "9:16"})
    api.runner.tick()
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "Zorblax" not in joined and KEY not in joined


# ---- plan jobs (Gemini writes scene prompts) ------------------------------------------
def test_plan_job_returns_scene_prompts(api):
    r = api.post("/plans", headers=AUTH, json={"prompt": "a lighthouse story", "aspect": "9:16", "scenes": 3})
    assert r.status_code == 200
    jid = r.json()["job_id"]
    api.runner.tick()
    body = api.get(f"/videos/{jid}", headers=AUTH).json()
    assert body["status"] == "done" and body["video_url"] is None
    assert body["scenes"] == [f"a lighthouse story (part {i})" for i in (1, 2, 3)]
    req = api.runner.generator.calls[0]
    assert req.scene_count == 3 and req.clip_seconds == 10


@pytest.mark.parametrize("scenes", [0, 13, "3", True])
def test_plan_scene_count_validated(api, scenes):
    r = api.post("/plans", headers=AUTH, json={"prompt": "x", "aspect": "9:16", "scenes": scenes})
    assert r.status_code == 400


def test_limit_while_planning_pauses(ws, store, lock):
    runner = make_runner(ws, store, lock, [stop_error("limit")])
    jid = store.create("idea", "9:16", None, kind="plan", scene_count=2)
    runner.tick()
    assert store.get(jid)["status"] == "failed" and runner.paused()["kind"] == "limit_reached"


def test_old_job_database_gets_plan_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, ref TEXT, prompt TEXT NOT NULL, aspect TEXT NOT NULL, "
                "images TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL, error TEXT, warnings TEXT NOT NULL "
                "DEFAULT '[]', attempts INTEGER NOT NULL DEFAULT 0, interruptions INTEGER NOT NULL DEFAULT 0, "
                "clip_path TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, "
                "updated_at REAL NOT NULL)")
    con.execute("INSERT INTO jobs (id, prompt, aspect, status, created_at, updated_at) VALUES "
                "('old', 'p', '9:16', 'done', 0, 0)")
    con.commit()
    con.close()
    assert JobStore(path).get("old")["kind"] == "clip"


@pytest.mark.parametrize("text,expected", [
    ('JSON\n```json\n["a", "b"]\n```', ["a", "b"]),
    ('["a", "b", "c"]', ["a", "b"]),  # extra scenes are dropped
    # unescaped dialogue quotes inside the scenes, one scene per line
    ('[\n"He mutters: "ارے یار!". It lifts off.",\n"A man shouts: "Look", "up!" and points."\n]',
     ['He mutters: "ارے یار!". It lifts off.', 'A man shouts: "Look", "up!" and points.']),
])
def test_parse_scene_list(text, expected):
    from worker.gemini_browser import parse_scene_list
    assert parse_scene_list(text, 2) == expected


@pytest.mark.parametrize("text", ['["only one"]', "Here are your scenes: one, two", '["a", '])
def test_parse_scene_list_rejects(text):
    from worker.gemini_browser import parse_scene_list
    from worker.generator import GenerationError
    with pytest.raises(GenerationError):
        parse_scene_list(text, 2)
