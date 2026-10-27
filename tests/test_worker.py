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
    s = WorkerSettings(_env_file=None, STUDIO_DATA_PATH=tmp_path / "data", STUDIO_API_KEY=KEY, MAX_RETRIES=1,
                       RETRY_DELAY_SECONDS=0)
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
    ("I can create more images as soon as your limit resets. Check your usage in Settings.", "image_limit"),
    ("I seem to be encountering an error. Can I try something else for you?", "error"),
    ("I'm having a hard time fulfilling your request. Can I help you with something else instead?", "error"),
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
    assert body["scenes"] == [{"action": f"a lighthouse story (part {i})"} for i in (1, 2, 3)]
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
    ('JSON\n```json\n["a", "b"]\n```', [{"action": "a"}, {"action": "b"}]),
    ('["a", "b", "c"]', [{"action": "a"}, {"action": "b"}]),  # extra scenes are dropped
    # unescaped dialogue quotes inside the scenes, one scene per line
    ('[\n"He mutters: "ارے یار!". It lifts off.",\n"A man shouts: "Look", "up!" and points."\n]',
     [{"action": 'He mutters: "ارے یار!". It lifts off.'}, {"action": 'A man shouts: "Look", "up!" and points.'}]),
    # script objects
    ('[{"action": "a", "shot": {"framing": "wide"}}, {"dialogue": [{"name": "Ali", "line": "Hi"}]}]',
     [{"action": "a", "shot": {"framing": "wide"}}, {"dialogue": [{"name": "Ali", "line": "Hi"}]}]),
    # double quotes inside a value
    ('[{"action": "He says "hi" loudly"},\n{"action": "b", "dialogue": [{"name": "Ali", "line": "It\'s "fine""}]}]',
     [{"action": 'He says "hi" loudly'}, {"action": "b", "dialogue": [{"name": "Ali", "line": 'It\'s "fine"'}]}]),
])
def test_parse_script(text, expected):
    from worker.gemini_browser import parse_script
    assert parse_script(text, 2) == expected


@pytest.mark.parametrize("text", ['["only one"]', "Here are your scenes: one, two", '["a", ', '[{"a": ""}, {}]'])
def test_parse_script_rejects(text):
    from worker.gemini_browser import parse_script
    from worker.generator import GenerationError
    with pytest.raises(GenerationError):
        parse_script(text, 2)


def test_plan_instruction_carries_cast_style_and_shot_vocabulary():
    from app import script
    from worker import gemini_browser as gb
    from worker.generator import PlanRequest
    for name in ("FRAMINGS", "ANGLES", "MOVEMENTS", "LENSES", "POSITIONS", "MAX_SPOKEN_WORDS"):
        assert getattr(gb, name) == getattr(script, name)  # Gemini and the editor use the same words
    text = gb.plan_instruction(PlanRequest("j", "a kite story", "9:16", 3, cast=[
        {"name": "Ali", "appearance": "tall man, beard", "outfit": "white kurta", "voice": "deep, calm"}],
        style="Anime", language="Urdu"))
    assert "- Ali: tall man, beard; white kurta. Voice: deep, calm" in text
    assert "Visual style for every scene: Anime" in text and "All spoken lines are in Urdu." in text
    assert "exactly 3 consecutive scenes" in text and "over-the-shoulder" in text and text.endswith("<<<\na kite story\n>>>")
    assert "at most one slow full turn per scene" in text and "use movement 'orbit'" in text
    assert "At most 3 people or animals in a shot" in text and "never 'a group of friends'" in text
    assert "starts where the previous one ended" in text and "One camera movement per shot" in text
    assert "scene 1 = 0-10 s, scene 2 = 10-20 s, scene 3 = 20-30 s" in text and "keep every moment" in text
    assert "Leave out a cast member the idea has no fitting role for" in text and "12-year-old is not the baby" in text
    assert "list everyone who is seen in it, the ones you invented too" in text
    assert "begin every scene's setting with that style" not in text  # a style was given
    six = [{"name": f"P{i}"} for i in range(6)]
    one = gb.plan_instruction(PlanRequest("j", "x", "9:16", 1, cast=six))
    assert "At most 6 people or animals in a shot. Spread the cast" in one
    assert "begin every scene's setting with that style" in one
    assert "Invent the characters" in gb.plan_instruction(PlanRequest("j", "x", "9:16", 1))


def test_plan_job_passes_cast_and_style_to_gemini(api):
    cast = [{"name": "Ali", "appearance": "tall", "voice": "deep", "extra": "dropped"}]
    r = api.post("/plans", headers=AUTH, json={"prompt": "story", "aspect": "9:16", "scenes": 2, "cast": cast,
                                               "style": "Anime", "language": "Urdu"})
    assert r.status_code == 200
    api.runner.tick()
    req = api.runner.generator.calls[0]
    assert req.cast == [{"name": "Ali", "appearance": "tall", "outfit": "", "voice": "deep", "personality": ""}]
    assert (req.style, req.language) == ("Anime", "Urdu")


@pytest.mark.parametrize("cast", ["Ali", [{"appearance": "no name"}], [{"name": "x"}] * 9])
def test_plan_cast_validated(api, cast):
    r = api.post("/plans", headers=AUTH, json={"prompt": "x", "aspect": "9:16", "scenes": 1, "cast": cast})
    assert r.status_code == 400


# ---- portrait jobs (Gemini draws a character) ------------------------------------------
def test_portrait_job_returns_an_image(api):
    r = api.post("/portraits", headers=AUTH, json={"description": "man in his 30s, beard", "style": "Anime"})
    assert r.status_code == 200
    jid = r.json()["job_id"]
    api.runner.tick()
    body = api.get(f"/videos/{jid}", headers=AUTH).json()
    assert body["status"] == "done" and body["image_url"] == f"/videos/{jid}/file" and body["video_url"] is None
    f = api.get(body["image_url"], headers=AUTH)
    assert f.headers["content-type"] == "image/png" and f.content.startswith(b"\x89PNG")
    prompt = api.runner.generator.calls[0].prompt
    assert "man in his 30s, beard" in prompt and "Style: Anime" in prompt and "3:4" in prompt


def test_portrait_needs_a_description(api):
    assert api.post("/portraits", headers=AUTH, json={"description": " "}).status_code == 400


def test_portrait_refused_while_paused(api):
    api.runner.pause("limit_reached", "Gemini's limit has been reached.")
    r = api.post("/portraits", headers=AUTH, json={"description": "x"})
    assert r.status_code == 409


def test_image_limit_fails_the_portrait_without_pausing(ws, store, lock):
    from worker.gemini_browser import image_limit_error
    err = image_limit_error("Limit reached. Some features aren't available until your limit resets on Oct 21 at "
                            "1:25 PM. Upgrade for higher limits.")
    assert "(Oct 21 at 1:25 PM)" in err.user_message
    runner = make_runner(ws, store, lock, [err])
    jid = store.create("draw", "3:4", None, kind="portrait")
    clip = store.create("a paper boat", "9:16", None)
    runner.tick()
    assert store.get(jid)["status"] == "failed" and "image limit" in store.get(jid)["error"]
    assert runner.paused() is None and store.get(jid)["attempts"] == 0
    runner.tick()
    assert store.get(clip)["status"] == "done"  # videos carry on
    assert "resets;" in image_limit_error("no banner").user_message


def test_limit_while_drawing_pauses(ws, store, lock):
    runner = make_runner(ws, store, lock, [stop_error("limit")])
    jid = store.create("draw", "3:4", None, kind="portrait")
    runner.tick()
    assert store.get(jid)["status"] == "failed" and runner.paused()["kind"] == "limit_reached"


def test_portrait_instruction():
    from worker.gemini_browser import portrait_instruction
    text = portrait_instruction("woman, curly hair. Outfit: yellow dupatta", "")
    assert "woman, curly hair. Outfit: yellow dupatta" in text and "Realistic photo" in text and "no text" in text
    assert "head to the waist" in text


def test_portrait_shows_full_body_when_footwear_is_described():
    from worker.gemini_browser import portrait_instruction
    for outfit in ("black pent , orange t-shirt, black shoes", "fancy heel", "gold khussa", "Sneakers"):
        text = portrait_instruction(f"man, curly hair. Outfit: {outfit}")
        assert "feet fully visible" in text and "waist" not in text, outfit
    assert "waist" in portrait_instruction("man. Outfit: shoelace-patterned shirt")  # a word, not footwear


def test_plan_read_from_geminis_side_by_side_vote():
    from worker.gemini_browser import ab_choice_text, parse_script
    survey = load_ui()["ab_survey"]
    body = ('New chat\nA Mother\'s Sacrifice\nYou are a film director…\nWhich response is more helpful?\n'
            'Your choice will help Gemini give you more helpful responses.\n'
            'Choice A\n[{"setting": "a small room", "action": "Amina sews"}, {"setting": "a stage", "action": "Zainab bows"}]\n'
            'Choice B\n[{"setting": "a humble room", "action": "Fatima sews"}]\nAsk Gemini\nFlash')
    scenes = parse_script(ab_choice_text(body, survey), 2)
    assert [s["action"] for s in scenes] == ["Amina sews", "Zainab bows"]
    assert ab_choice_text("Here is your script: [ ... ]", survey) is None


# ---- story bible and video-type rules -----------------------------------------------------
def test_parse_plan_reads_bible_and_scenes():
    from worker.gemini_browser import parse_plan
    text = ('```json\n{"bible": {"title": "T", "characters": [{"name": "Fi", "look": "bunny"}]},\n'
            '"scenes": [\n{"action": "she says "hi""},\n{"action": "b"}, {"action": "c"}]}\n```')
    assert parse_plan(text, 2) == {"bible": {"title": "T", "characters": [{"name": "Fi", "look": "bunny"}]},
                                   "scenes": [{"action": 'she says "hi"'}, {"action": "b"}]}
    assert parse_plan('["a", "b"]', 2) == {"bible": {}, "scenes": [{"action": "a"}, {"action": "b"}]}


@pytest.mark.parametrize("text", ['{"bible": {}, "scenes": [{"action": "a"}]}', '{"bible": {}}', '{"bible": {',
                                  "no json here"])
def test_parse_plan_rejects(text):
    from worker.gemini_browser import parse_plan
    from worker.generator import GenerationError
    with pytest.raises(GenerationError):
        parse_plan(text, 2)


def test_plan_instruction_asks_for_bible_and_carries_type_rules():
    from worker import gemini_browser as gb
    from worker.generator import PlanRequest
    text = gb.plan_instruction(PlanRequest("j", "idea", "9:16", 3, rules="- This is a kids video."))
    assert "Rules for this kind of video:\n- This is a kids video.\n" in text
    assert '{"bible": {...}, "scenes": [...]}' in text and '"palette": "..."' in text
    assert "Scene 1 hooks the viewer" in text and "leads into the next" in text
    assert "fix each character's look once in the bible" in text
    cast = gb.plan_instruction(PlanRequest("j", "idea", "9:16", 2, cast=[{"name": "Female", "outfit": "lehenga"}]))
    assert "where the idea itself says a cast member's age, look, clothes or voice, the idea wins" in cast
    assert '"cast": [{"name": "...", "age": "...", "look": "...", "outfit": "...", "voice": "..."}]' in cast
    assert "Never drop anything the idea writes out" in text and "more scenes than these 3" in text
    assert "except lines the idea writes out" in text
    assert "Rules for this kind of video" not in gb.plan_instruction(PlanRequest("j", "x", "9:16", 1))


def test_plan_job_passes_rules_and_returns_bible(api):
    def plan(req, should_cancel):
        return {"bible": {"title": "T"}, "scenes": [{"action": "a"}, {"action": "b"}]}
    api.runner.generator.plan = plan
    r = api.post("/plans", headers=AUTH, json={"prompt": "story", "aspect": "9:16", "scenes": 2,
                                               "rules": "- be kind"})
    jid = r.json()["job_id"]
    assert api.runner.store.get(jid)["options"]["rules"] == "- be kind"
    api.runner.tick()
    body = api.get(f"/videos/{jid}", headers=AUTH).json()
    assert body["bible"] == {"title": "T"} and body["scenes"] == [{"action": "a"}, {"action": "b"}]


# ---- the Video tool's aspect menu ---------------------------------------------------------
class _Btn:
    def __init__(self, page, text):
        self.p, self.text = page, text

    def inner_text(self):
        return self.p.state if self.text is None else self.text

    def click(self):
        self.p.clicks.append(self.inner_text())
        if self.text is not None:  # an option: the switch now shows it
            self.p.state = self.text.split(" ")[0]


class _AspectPage:
    """Just enough of gemini_browser.Page for _choose_aspect: a switch showing `state`, and its menu."""

    def __init__(self, state="Landscape", has_menu=True, options=("Landscape (16:9)", "Portrait (9:16)")):
        self.state, self.has_menu, self.options, self.clicks = state, has_menu, options, []
        self.page = type("P", (), {"wait_for_timeout": lambda *_: None,
                                   "keyboard": type("K", (), {"press": lambda *_: None})()})()

    def find(self, key, timeout=5.0, **fmt):
        if key == "aspect_switch":
            return _Btn(self, None) if self.has_menu else None
        return next((_Btn(self, o) for o in self.options if fmt["label"] in o), None)

    def screenshot(self, *a):
        pass


def _gemini(ws):
    from worker import gemini_browser
    g = gemini_browser.GeminiBrowserGenerator.__new__(gemini_browser.GeminiBrowserGenerator)
    g.settings = ws
    return g


def test_aspect_menu_switches_to_portrait(ws):
    pg = _AspectPage()
    _gemini(ws)._choose_aspect(pg, "9:16")
    assert pg.state == "Portrait" and pg.clicks == ["Landscape", "Portrait (9:16)"]
    pg = _AspectPage(state="Portrait")
    _gemini(ws)._choose_aspect(pg, "9:16")
    assert pg.clicks == []  # already set: nothing clicked


def test_aspect_menu_missing(ws):
    from worker.generator import GenerationError
    _gemini(ws)._choose_aspect(_AspectPage(has_menu=False), "16:9")  # Gemini's default: carry on
    with pytest.raises(GenerationError, match="aspect ratio menu"):
        _gemini(ws)._choose_aspect(_AspectPage(has_menu=False), "9:16")
    with pytest.raises(GenerationError, match="no Portrait"):
        _gemini(ws)._choose_aspect(_AspectPage(options=("Landscape (16:9)",)), "9:16")


def test_ui_config_has_aspect_menu():
    ui = load_ui()
    assert ui["selectors"]["aspect_switch"] and "{label}" in ui["selectors"]["aspect_option"][0]


# ---- planner stalls and misroutes ---------------------------------------------------------
def test_plan_instruction_is_text_only_and_fences_the_idea():
    from worker import gemini_browser as gb
    from worker.generator import PlanRequest
    text = gb.plan_instruction(PlanRequest("j", "STRICT VIDEO RULES: exactly 15 seconds", "9:16", 2))
    assert text.startswith("TEXT ONLY: this is a writing task. Do not create")
    assert "not a request to you" in text and text.endswith("<<<\nSTRICT VIDEO RULES: exactly 15 seconds\n>>>")


@pytest.mark.parametrize("text, misrouted", [
    ("I'm generating your video. This could take a few minutes, so check back to see when your video is ready.", True),
    ('{"bible": {}, "scenes": [{"action": "the video starts"}]}', False),
])
def test_plan_misroute_detected(text, misrouted):
    assert StopText(load_ui()).misrouted(text) is misrouted


def test_plan_gets_one_more_attempt_than_a_clip(ws, store, lock):
    from worker.generator import GenerationError
    runner = make_runner(ws, store, lock, [GenerationError("stalled")] * 3)
    jid = store.create("idea", "9:16", None, kind="plan", scene_count=2)
    for _ in range(3):
        runner.tick()
    job = store.get(jid)
    assert job["status"] == "failed" and job["attempts"] == 3 and len(runner.generator.calls) == 3


def test_plan_model_per_attempt(tmp_path):
    s = WorkerSettings(_env_file=None, STUDIO_DATA_PATH=tmp_path, STUDIO_API_KEY=KEY)
    assert [s.plan_model(i) for i in (1, 2, 3)] == ["Flash", "Pro", "Flash"]
    assert [s.plan_model(i, last_good="Pro") for i in (1, 2, 3)] == ["Pro", "Flash", "Pro"]
    assert s.plan_model(1, last_good="Ultra") == "Flash"
    s = WorkerSettings(_env_file=None, STUDIO_DATA_PATH=tmp_path, STUDIO_API_KEY=KEY, GEMINI_PLAN_MODELS="Pro, Thinking")
    assert [s.plan_model(i) for i in (1, 2, 3)] == ["Pro", "Thinking", "Pro"]
    assert WorkerSettings(_env_file=None, STUDIO_DATA_PATH=tmp_path, STUDIO_API_KEY=KEY,
                          GEMINI_PLAN_MODELS="").plan_model(1) == ""


def test_plan_retry_counts_its_attempt(ws, store, lock):
    from worker.generator import GenerationError
    runner = make_runner(ws, store, lock, [GenerationError("stalled")])
    store.create("idea", "9:16", None, kind="plan", scene_count=2)
    runner.tick()
    runner.tick()
    assert [c.attempt for c in runner.generator.calls] == [1, 2]


@pytest.mark.parametrize("text", ["I encountered an error doing what you asked. Could you try again?",
                                  "Sorry, something went wrong. Please try your request again."])
def test_gemini_chat_errors_are_recognised(text):
    assert StopText(load_ui()).classify(text) == "error"


def test_ui_config_knows_a_busy_reply():
    assert any("Answer now" in s for s in load_ui()["selectors"]["reply_busy"])


def test_real_people_refusal_is_a_safety_stop_not_a_retry():
    from worker.generator import SafetyRefusal
    text = "I can't make videos of real people in situations like that. Can I help you with something else?"
    assert StopText(load_ui()).classify(text) == "real_people"
    err = stop_error("real_people")
    assert isinstance(err, SafetyRefusal) and "cartoon-style portraits" in err.user_message


def test_retry_waits_before_running_again(ws, store, lock):
    from worker.generator import GenerationError
    ws.RETRY_DELAY_SECONDS = 90
    runner = make_runner(ws, store, lock, [GenerationError("I encountered an error")])
    jid = store.create("a clip", "9:16", None)
    runner.tick()
    assert store.get(jid)["status"] == "queued" and store.next_queued() is None  # held back
    store.update(jid, not_before=time.time() - 1)
    assert store.next_queued()["id"] == jid


def test_gemini_error_message_says_it_is_geminis():
    assert "comes from Gemini" in stop_error("error").user_message


@pytest.mark.parametrize("text, label, named", [
    ("Flash", "Flash", True), ("3.8 Flash", "Flash", True), ("3.5 Flash-Lite", "Flash", False),
    ("Flash-Lite", "Flash-Lite", True), ("3.1 Pro", "Pro", True), ("Production", "Pro", False),
])
def test_model_named_exactly(text, label, named):
    from worker.gemini_browser import model_named
    assert model_named(text, label) is named


def test_plan_starts_on_the_model_that_last_worked(ws, store, lock):
    from worker.generator import GenerationError
    runner = make_runner(ws, store, lock, [GenerationError("Flash is down")])
    store.create("idea", "9:16", None, kind="plan", scene_count=2)
    runner.tick()
    runner.tick()  # attempt 2, on Pro, works
    assert [c.model for c in runner.generator.calls] == ["Flash", "Pro"]
    assert store.kv_get("plan_model_ok") == "Pro"
    store.create("next idea", "9:16", None, kind="plan", scene_count=2)
    runner.tick()
    assert runner.generator.calls[-1].model == "Pro"



# ---- scene stills and clip checks ---------------------------------------------------------------
def test_still_job_attaches_pictures_and_asks_for_a_first_frame(api):
    import json as _json
    r = api.post("/stills", headers=AUTH, data={"body": _json.dumps({
        "description": "Ali runs on a rooftop.", "aspect": "9:16", "names": ["Ali"], "previous": True})},
        files=[("images", ("a.png", PNG, "image/png")), ("images", ("f.jpg", JPG, "image/jpeg"))])
    assert r.status_code == 200, r.text
    api.runner.tick()
    req = api.runner.generator.calls[0]
    assert len(req.images) == 2 and "Vertical 9:16 image" in req.prompt
    assert "picture 1 is Ali" in req.prompt and "Picture 2 is the last frame of the previous shot" in req.prompt
    body = api.get(f"/videos/{r.json()['job_id']}", headers=AUTH).json()
    assert body["status"] == "done" and body["image_url"]


def test_check_job_watches_the_clips_with_named_files(api):
    import json as _json
    clip = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
    r = api.post("/checks", headers=AUTH, data={"body": _json.dumps({
        "names": ["Saas"], "previous": True, "action": "Bahu hides the phone.",
        "lines": [{"name": "Saas", "line": "Ni nuhay!", "position": "frame left"}]})},
        files=[("images", ("s.png", PNG, "image/png")), ("videos", ("p.mp4", clip, "video/mp4")),
               ("videos", ("n.mp4", clip, "video/mp4")), ("stills", ("f1.jpg", JPG, "image/jpeg")),
               ("stills", ("f2.jpg", JPG, "image/jpeg"))])
    assert r.status_code == 200, r.text
    api.runner.tick()
    req = api.runner.generator.calls[0]
    assert [p.name for p in req.images] == ["Saas.png", "previous-shot.mp4", "new-shot.mp4", "new-shot-frame-1.jpg",
                                            "new-shot-frame-2.jpg"]
    assert "In each still new-shot-frame-1 to new-shot-frame-2".lower() in req.prompt.lower()
    assert "count every hand you can see" in req.prompt
    assert req.prompt.startswith("TEXT ONLY: this is a checking task") and "What should be said" in req.prompt
    assert "Saas (frame left) says: 'Ni nuhay!'" in req.prompt and "story_happened" in req.prompt
    assert "lips_in_sync" in req.prompt and "same_voices" in req.prompt
    assert api.get(f"/videos/{r.json()['job_id']}", headers=AUTH).json()["check"] == {"ok": True, "problems": ""}
    from worker import gemini_browser as gb
    asked = gb.check_instruction(["Saas"], False, "x", [], {"Saas": "an orange shalwar kameez", "Zed": "a hat"})
    assert "Saas should wear an orange shalwar kameez instead of the clothes in their picture" in asked
    assert "Zed" not in asked  # not in this shot
    bad = api.post("/checks", headers=AUTH, data={"body": _json.dumps({"names": ["Saas"]})},
                   files=[("images", ("1.png", PNG, "image/png"))])
    assert bad.status_code == 400  # no clip


@pytest.mark.parametrize("text, verdict", [
    ('{"same_people": true, "same_clothes": true, "same_place": true, "problems": ""}',
     {"ok": True, "problems": "", "failed": []}),
    ('Sure: {"same_people": true, "same_clothes": false, "problems": "hoodie is grey"}',
     {"ok": False, "problems": "hoodie is grey", "failed": ["same_clothes"]}),
    ('{"same_people": false, "same_clothes": true, "same_place": true, "problems": "different face"}',
     {"ok": False, "problems": "different face", "failed": ["same_people"]}),
])
def test_parse_check(text, verdict):
    from worker.gemini_browser import parse_check
    assert parse_check(text) == verdict


@pytest.mark.parametrize("text", ["looks fine to me", '{"problems": "none"}', "{not json}"])
def test_parse_check_rejects(text):
    from worker.gemini_browser import parse_check
    from worker.generator import GenerationError
    with pytest.raises(GenerationError):
        parse_check(text)


def test_check_catches_extra_hands_and_planner_knows_two_hands():
    from worker import gemini_browser as gb
    from worker.generator import PlanRequest
    assert gb.parse_check('{"same_people": true, "same_clothes": true, "normal_bodies": false, '
                          '"problems": "Saas has a third hand"}') == {"ok": False, "problems": "Saas has a third hand",
                                                                      "failed": ["normal_bodies"]}
    assert "two arms, two hands, five fingers" in gb.check_instruction(["Ali"], False, "Ali waves.")
    assert "People have two hands" in gb.plan_instruction(PlanRequest("j", "x", "9:16", 2))


@pytest.mark.parametrize("key", ["story_happened", "right_speakers", "lips_in_sync", "same_voices"])
def test_check_fails_on_story_speaker_lips_and_voice(key):
    from worker.gemini_browser import parse_check
    assert parse_check(f'{{"same_people": true, "{key}": false, "problems": "x"}}')["ok"] is False


def test_ui_config_knows_the_rights_reminder():
    assert any("reminder about creating videos" in s and "Agree" in s
               for s in load_ui()["selectors"]["rights_reminder_agree"])


def test_full_capacity_is_retried_after_a_longer_wait(ws, store, lock):
    from worker.generator import GeminiBusy
    assert StopText(load_ui()).classify("I'm sorry, I'm experiencing unusually high traffic right now generating "
                                        "videos and am at full capacity. Please try again later.") == "busy"
    assert isinstance(stop_error("busy"), GeminiBusy)
    ws.RETRY_DELAY_SECONDS = 90
    runner = make_runner(ws, store, lock, [stop_error("busy")])
    jid = store.create("a clip", "9:16", None)
    started = time.time()
    runner.tick()
    job = store.get(jid)
    assert job["status"] == "queued" and job["not_before"] >= started + 3 * 90 - 1


def test_planner_keeps_objects_and_pace():
    from worker import gemini_browser as gb
    from worker.generator import PlanRequest
    text = gb.plan_instruction(PlanRequest("j", "x", "9:16", 2))
    assert "Everything keeps its state from moment to moment and scene to scene" in text
    assert "in which hand" in text and "doors and windows" in text and "At most one fast movement" in text
    assert "even a 'young man'), they are an adult and must look" in text and "late 20s or older" in text
    assert "never list a beard, mustache or stubble under 'never'" in text
