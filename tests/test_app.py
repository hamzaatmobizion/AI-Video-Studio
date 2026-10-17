import re
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

from app.enums import GenStatus
from app.models import Generation, GoogleConnection, utcnow
from app.providers.drive_storage import _token_cache
from app.routes.api import duration_from_prompt
from tests.conftest import csrf_of
from tests.fakes import PNG


def tick(app, n=1):
    for _ in range(n):
        app.state.service.tick()


def gen(app, gid) -> Generation:
    with app.state.db.session() as s:
        return s.get(Generation, gid)


def submit(client, prompt="a paper boat at dusk", aspect="9:16", files=None):
    return client.post("/generations", data={"prompt": prompt, "aspect": aspect}, files=files or [])


def setup_function():
    _token_cache.clear()


# ---- auth / CSRF -----------------------------------------------------------------
def test_migrations_create_tables(app):
    names = set(inspect(app.state.db.engine).get_table_names())
    assert {"users", "generations", "google_connection", "alembic_version"} <= names


def test_pages_need_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/api/generations").status_code == 401


def test_login_requires_csrf_and_password(client, user):
    assert client.post("/login", json={"email": "owner@example.com", "password": "x"}).status_code == 403
    token = csrf_of(client)
    bad = client.post("/login", json={"email": "owner@example.com", "password": "wrong"},
                      headers={"X-CSRF-Token": token})
    assert bad.status_code == 400
    ok = client.post("/login", json={"email": "OWNER@example.com ", "password": "correct horse battery"},
                     headers={"X-CSRF-Token": token})
    assert ok.status_code == 200
    assert client.get("/").status_code == 200


def test_post_without_csrf_rejected(authed):
    token = authed.headers.pop("X-CSRF-Token")
    assert submit(authed).status_code == 403
    authed.headers["X-CSRF-Token"] = token


def test_change_password(authed):
    def change(cur, new):
        return authed.post("/account/password", json={"current_password": cur, "new_password": new})

    assert change("wrong", "a brand new password").status_code == 400
    assert change("correct horse battery", "short").status_code == 400
    assert change("correct horse battery", "correct horse battery").status_code == 400
    assert change("correct horse battery", "a brand new password").status_code == 200
    authed.post("/logout")
    token = csrf_of(authed)
    login = lambda pw: authed.post("/login", json={"email": "owner@example.com", "password": pw},  # noqa: E731
                                   headers={"X-CSRF-Token": token})
    assert login("correct horse battery").status_code == 400
    assert login("a brand new password").status_code == 200


def test_account_is_its_own_tab(authed):
    page = authed.get("/account").text
    assert 'id="password-form"' in page and "owner@example.com" in page and "Member since" in page
    assert 'href="/account" class="active"' in page
    assert 'id="password-form"' not in authed.get("/settings").text
    assert 'href="/settings" class="active"' in authed.get("/settings").text


def test_change_password_needs_login_and_csrf(client, user):
    body = {"current_password": "correct horse battery", "new_password": "a brand new password"}
    assert client.post("/account/password", json=body).status_code == 401


def signup(client, email, password="a long enough password"):
    return client.post("/signup", json={"email": email, "password": password},
                       headers={"X-CSRF-Token": csrf_of(client)})


def test_signup_creates_account_and_signs_in(client):
    assert client.get("/signup").status_code == 200
    assert client.post("/signup", json={"email": "new@example.com", "password": "a long enough password"}
                       ).status_code == 403  # no CSRF
    assert signup(client, "not-an-email").status_code == 400
    assert signup(client, "new@example.com", "short").status_code == 400
    assert signup(client, " New@Example.com ").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/signup", follow_redirects=False).status_code == 303


def test_signup_rejects_existing_email(client, user):
    r = signup(client, "OWNER@example.com")
    assert r.status_code == 400 and "already exists" in r.json()["detail"]


def test_users_only_see_their_own_videos_and_drive(app, authed, user, drive_connected, net):
    from fastapi.testclient import TestClient

    clip = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 40
    net.worker.video_bytes = clip
    gid = submit(authed).json()["id"]
    net.worker.jobs["job1"]["status"] = "done"
    tick(app)
    assert gen(app, gid).status == GenStatus.READY and gen(app, gid).user_id == user

    other = TestClient(app)
    assert signup(other, "other@example.com").status_code == 200
    other.headers["X-CSRF-Token"] = csrf_of(other)
    assert other.get("/api/generations").json()["cards"] == []
    assert other.get(f"/generations/{gid}/file").status_code == 404
    for action in ("cancel", "retry", "dismiss", "save"):
        assert other.post(f"/generations/{gid}/{action}").status_code == 404
    assert "Not connected" in other.get("/settings").text  # owner's Drive isn't shared
    r = submit(other)
    assert r.status_code == 503 and "Google Drive" in r.json()["detail"]
    assert gen(app, gid).status == GenStatus.READY and not gen(app, gid).dismissed
    assert authed.get(f"/generations/{gid}/file").status_code == 200


def test_logout(authed):
    assert authed.post("/logout").status_code == 200
    assert authed.get("/api/generations").status_code == 401


# ---- generation flow --------------------------------------------------------------
def test_needs_drive_before_generating(authed):
    r = submit(authed)
    assert r.status_code == 503 and "Google Drive" in r.json()["detail"]


def test_rejects_fake_image_by_content(authed, drive_connected):
    r = submit(authed, files=[("images", ("cat.png", b"not an image at all", "image/png"))])
    assert r.status_code == 400 and "isn't a JPG, PNG or WebP" in r.json()["detail"]


def test_rejects_too_many_images(authed, drive_connected):
    files = [("images", (f"{i}.png", PNG, "image/png")) for i in range(4)]
    assert submit(authed, files=files).status_code == 400


def test_full_flow_to_ready_and_range_stream(app, authed, drive_connected, net):
    clip = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 40
    net.worker.video_bytes = clip
    r = submit(authed, files=[("images", ("ref.png", PNG, "image/png"))])
    assert r.status_code == 200
    gid = r.json()["id"]
    assert net.worker.submits[0]["multipart"] is True
    g = gen(app, gid)
    assert g.status == GenStatus.GENERATING and g.worker_job_id == "job1"

    net.worker.jobs["job1"]["status"] = "running"
    tick(app)
    assert gen(app, gid).status == GenStatus.GENERATING

    net.worker.jobs["job1"]["status"] = "done"
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.READY, g.error
    assert g.temp_path is None
    assert not any(Path(app.state.settings.generated_dir).iterdir())  # temp copy deleted

    stored = net.google.files[g.drive_file_id]
    assert stored["data"] == clip
    assert re.fullmatch(r"\d{4}-\d\d-\d\d a paper boat at dusk \(\d+\)\.mp4", stored["name"])
    month = net.google.files[stored["parents"][0]]
    assert re.fullmatch(r"\d{4}-\d\d", month["name"])
    root = net.google.files[month["parents"][0]]
    assert root["name"] == "VideoGeneration"
    with app.state.db.session() as s:
        assert s.scalar(select(GoogleConnection)).folder_id == month["parents"][0]

    full = authed.get(f"/generations/{gid}/file")
    assert full.status_code == 200 and full.content == clip
    part = authed.get(f"/generations/{gid}/file", headers={"Range": "bytes=10-19"})
    assert part.status_code == 206
    assert part.content == clip[10:20]
    assert part.headers["content-range"] == f"bytes 10-19/{len(clip)}"

    cards = authed.get("/api/generations").json()
    assert cards["active"] is False and f"/generations/{gid}/file" in cards["cards"][0]["html"]


def test_worker_4xx_on_submit_fails_with_its_message(app, authed, drive_connected, net):
    net.worker.submit_response = (409, {"detail": "Gemini's video limit has been reached."})
    gid = submit(authed).json()["id"]
    g = gen(app, gid)
    assert g.status == GenStatus.FAILED and g.error == "Gemini's video limit has been reached."


def test_worker_failed_status_shown_verbatim(app, authed, drive_connected, net):
    gid = submit(authed).json()["id"]
    net.worker.jobs["job1"].update(status="failed", error="Gemini declined this prompt for safety reasons.")
    tick(app)
    assert gen(app, gid).error == "Gemini declined this prompt for safety reasons."


def test_ten_consecutive_poll_errors_fail(app, authed, drive_connected, net):
    gid = submit(authed).json()["id"]
    net.worker.status_errors = 9
    tick(app, 9)
    assert gen(app, gid).status == GenStatus.GENERATING
    net.down = True
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.FAILED and "Lost contact" in g.error


def test_poll_error_counter_resets_on_success(app, authed, drive_connected, net):
    gid = submit(authed).json()["id"]
    net.worker.status_errors = 5
    tick(app, 6)
    assert gen(app, gid).poll_errors == 0


def test_timeout_cancels_job(app, authed, drive_connected, net):
    gid = submit(authed).json()["id"]
    with app.state.db.session() as s:
        s.get(Generation, gid).submitted_at = utcnow() - timedelta(minutes=31)
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.FAILED and "didn't finish in time" in g.error
    assert net.worker.cancelled == ["job1"]


def test_non_video_download_fails(app, authed, drive_connected, net):
    net.worker.video_bytes = b"<html>oops</html>"
    gid = submit(authed).json()["id"]
    net.worker.jobs["job1"]["status"] = "done"
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.FAILED and "isn't an MP4" in g.error


def _ready_to_save(app, authed, net):
    net.worker.video_bytes = b"\x00\x00\x00\x18ftypmp42" + b"x" * 1000
    gid = submit(authed).json()["id"]
    net.worker.jobs["job1"]["status"] = "done"
    return gid


def test_upload_retry_keeps_temp_then_succeeds(app, authed, drive_connected, net):
    gid = _ready_to_save(app, authed, net)
    net.google.upload_failures = [503]
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.SAVING and g.upload_attempts == 1 and Path(g.temp_path).exists()
    tick(app)  # not yet: waits 30 s
    assert gen(app, gid).upload_attempts == 1
    with app.state.db.session() as s:
        s.get(Generation, gid).next_upload_at = utcnow()
    tick(app)
    assert gen(app, gid).status == GenStatus.READY


def test_upload_gives_up_after_ten(app, authed, drive_connected, net):
    gid = _ready_to_save(app, authed, net)
    net.google.upload_failures = [503] * 10
    tick(app)
    for _ in range(9):
        with app.state.db.session() as s:
            s.get(Generation, gid).next_upload_at = utcnow()
        tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.SAVE_FAILED and g.upload_attempts == 10 and Path(g.temp_path).exists()


def test_drive_full_then_try_saving_again(app, authed, drive_connected, net):
    gid = _ready_to_save(app, authed, net)
    net.google.quota_full = True
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.SAVE_FAILED and "Drive is full" in g.error
    submits = len(net.worker.submits)
    net.google.quota_full = False
    assert authed.post(f"/generations/{gid}/save").status_code == 200
    tick(app)
    assert gen(app, gid).status == GenStatus.READY
    assert len(net.worker.submits) == submits  # no regeneration


def test_md5_mismatch_is_retried(app, authed, drive_connected, net):
    gid = _ready_to_save(app, authed, net)
    net.google.corrupt_md5 = True
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.SAVING and "didn't match" in g.error


def test_cancel_ready_moves_to_trash(app, authed, drive_connected, net):
    gid = _ready_to_save(app, authed, net)
    tick(app)
    fid = gen(app, gid).drive_file_id
    assert authed.post(f"/generations/{gid}/cancel").status_code == 200
    assert net.google.files[fid]["trashed"] is True
    assert gen(app, gid).status == GenStatus.CANCELLED


def test_cancel_generating_tells_worker(app, authed, drive_connected, net):
    gid = submit(authed).json()["id"]
    authed.post(f"/generations/{gid}/cancel")
    assert net.worker.cancelled == ["job1"] and gen(app, gid).status == GenStatus.CANCELLED


def test_retry_and_dismiss(app, authed, drive_connected, net):
    net.worker.submit_response = (400, {"detail": "nope"})
    gid = submit(authed).json()["id"]
    net.worker.submit_response = None
    assert authed.post(f"/generations/{gid}/retry").status_code == 200
    assert gen(app, gid).status == GenStatus.GENERATING
    assert authed.post(f"/generations/{gid}/dismiss").status_code == 400  # still active
    authed.post(f"/generations/{gid}/cancel")
    assert authed.post(f"/generations/{gid}/dismiss").status_code == 200
    assert authed.get("/api/generations").json()["cards"] == []


# ---- Google connect ----------------------------------------------------------------
def test_google_oauth_state_checked(app, authed, net):
    r = authed.get("/auth/google/start", follow_redirects=False)
    assert r.status_code == 303 and "accounts.google.com" in r.headers["location"]
    state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
    bad = authed.get("/auth/google/callback?state=wrong&code=good-code", follow_redirects=False)
    assert "bad_state" in bad.headers["location"]
    # state was consumed by the failed attempt; start again
    r = authed.get("/auth/google/start", follow_redirects=False)
    state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
    ok = authed.get(f"/auth/google/callback?state={state}&code=good-code", follow_redirects=False)
    assert "connected" in ok.headers["location"]
    with app.state.db.session() as s:
        conn = s.scalar(select(GoogleConnection))
    assert conn.email == "owner@example.com"
    assert "rt-secret" not in conn.refresh_token_enc  # encrypted at rest
    assert app.state.providers.cipher().decrypt(conn.refresh_token_enc) == "rt-secret"


def test_google_connect_without_drive_scope_is_rejected(app, authed, net):
    r = authed.get("/auth/google/start", follow_redirects=False)
    state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
    res = authed.get(f"/auth/google/callback?state={state}&code=no-drive-code", follow_redirects=False)
    assert "missing_scope" in res.headers["location"]
    with app.state.db.session() as s:
        assert s.scalar(select(GoogleConnection)) is None
    assert net.google.revoked == ["rt-nodrive"]
    assert "Drive access wasn&#39;t granted" in authed.get(res.headers["location"]).text


def test_disconnect_revokes(app, authed, user, drive_connected, net):
    assert authed.post("/settings/google/disconnect").status_code == 200
    assert net.google.revoked == ["rt-secret"]
    assert app.state.providers.connection(user) is None


def test_folder_create_and_pick(app, authed, user, drive_connected, net):
    r = authed.post("/settings/google/folder", json={"new_name": "Reels"})
    assert r.status_code == 200
    fid = r.json()["folder"]["id"]
    folders = authed.post("/settings/google/folders").json()["folders"]
    assert {"id": fid, "name": "Reels"} in folders
    assert app.state.providers.connection(user).folder_name == "Reels"
    authed.post("/settings/google/folder/default")
    assert app.state.providers.connection(user).folder_id is None


def test_settings_page_renders_status(authed, drive_connected):
    html = authed.get("/settings").text
    assert "Connected as owner@example.com" in html
    assert "Gemini" in html and "Connected" in html


def test_settings_shows_subscription_and_video_access(authed, net):
    net.worker.gemini.update(plan="free", video="unavailable")
    html = authed.get("/settings").text
    assert "Free plan" in html and "Not available on this account" in html and "Google AI Pro or Ultra" in html
    net.worker.gemini.update(plan="paid", video="available")
    html = authed.get("/settings").text
    assert "Paid plan" in html and "Video generation: <strong>Available" in html
    assert "Google AI Pro or Ultra plan. Upgrade" not in html
    assert "Use a different Google account" in html and "Open Gemini sign-in window" not in html
    net.worker.gemini.pop("plan"), net.worker.gemini.pop("video")
    assert "Click Check session to see" in authed.get("/settings").text
    net.worker.gemini.update(state="signed_out")
    html = authed.get("/settings").text
    assert "Open Gemini sign-in window" in html and "Use a different Google account" not in html


def test_settings_sign_in_window_has_no_done_step_and_polls(authed, net):
    net.worker.gemini.update(login_window_open=True)
    html = authed.get("/settings").text
    assert "quit that window with ⌘Q" in html and "Done signing in" not in html
    assert 'id="gemini-watch"' in html
    key = re.search(r'id="gemini-watch" data-key="([^"]+)"', html).group(1)
    assert authed.get("/settings/gemini/state").json() == {"key": key, "busy": True}
    net.worker.gemini.update(login_window_open=False, checking=True)
    assert "Checking your Gemini sign-in" in authed.get("/settings").text
    assert authed.get("/settings/gemini/state").json()["key"] != key


def test_settings_reports_missing_config(tmp_path, net):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    s = Settings(_env_file=None, SECRET_KEY="s" * 40, DATABASE_URL=f"sqlite:///{tmp_path}/a.db",
                 VAR_DIR=tmp_path / "v", SCHEDULER_ENABLED=False, WORKER_API_KEY="")
    assert any("WORKER_URL" in p for p in s.problems())
    assert any("GOOGLE_CLIENT_ID" in p for p in s.problems())


def test_refuses_to_start_without_secret(tmp_path):
    import pytest

    from app.config import Settings
    from app.main import create_app

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app(Settings(_env_file=None, SECRET_KEY="short", VAR_DIR=tmp_path))


# ---- history ------------------------------------------------------------------------
def seed(app, user_id, n=1, status=GenStatus.READY, prompt="a calm lake", **kw):
    ids = []
    with app.state.db.session() as s:
        for i in range(n):
            g = Generation(user_id=user_id, prompt=f"{prompt} {i}", aspect="9:16", ref_images=[], status=status,
                           poll_errors=0, upload_attempts=0, **{"dismissed": False, **kw})
            s.add(g)
            s.flush()
            ids.append(g.id)
    return ids


def test_history_tab_lists_everything_including_dismissed(app, authed, user):
    ready, failed = seed(app, user)[0], seed(app, user, status=GenStatus.FAILED, prompt="a stormy sea")[0]
    hidden = seed(app, user, status=GenStatus.CANCELLED, prompt="a dusty road", dismissed=True)[0]
    html = authed.get("/history").text
    assert 'href="/history" class="active"' in html and "3 videos" in html
    for gid in (ready, failed, hidden):
        assert f"#{gid} ·" in html
    assert "Hidden from Studio" in html and f"/generations/{hidden}/restore" in html
    assert f"/generations/{ready}/file?download=1" in html and f"/generations/{failed}/retry" in html
    assert f"#{hidden} ·" not in authed.get("/").text  # Studio still hides it


def test_history_search_filter_and_pages(app, authed, user):
    seed(app, user, 25)
    seed(app, user, 2, status=GenStatus.FAILED, prompt="100%_storm")
    assert "27 videos" in authed.get("/history").text
    assert "2 videos match" in authed.get("/history?status=failed").text
    assert "2 videos match" in authed.get("/history?q=100%25_").text  # % and _ are literal
    assert "0 videos match" in authed.get("/history?q=storm&status=ready").text
    assert "Nothing matches" in authed.get("/history?q=nope").text
    page2 = authed.get("/history?page=2").text
    assert "Page 2 of 2" in page2 and page2.count('class="history-row"') == 7 and "← Newer" in page2
    assert authed.get("/history?status=bogus&page=-3").status_code == 200


def test_history_empty_state(authed, user):
    assert "No videos yet" in authed.get("/history").text


def test_restore_delete_and_retry_undismisses(app, authed, user, drive_connected, net, tmp_path):
    gid = seed(app, user, status=GenStatus.FAILED, dismissed=True)[0]
    assert authed.post(f"/generations/{gid}/restore").status_code == 200
    assert gen(app, gid).dismissed is False
    authed.post(f"/generations/{gid}/dismiss")
    assert authed.post(f"/generations/{gid}/retry").status_code == 200
    g = gen(app, gid)
    assert g.dismissed is False and g.status == GenStatus.GENERATING
    assert authed.post(f"/generations/{gid}/delete").status_code == 400  # still in progress

    old = seed(app, user, status=GenStatus.CANCELLED)[0]
    refs = app.state.settings.refs_dir / str(old)
    refs.mkdir(parents=True)
    (refs / "ref-1.png").write_bytes(PNG)
    assert authed.post(f"/generations/{old}/delete").status_code == 200
    assert gen(app, old) is None and not refs.exists()


def test_history_is_per_user(app, authed, user):
    from fastapi.testclient import TestClient

    gid = seed(app, user)[0]
    other = TestClient(app)
    signup(other, "other@example.com")
    other.headers["X-CSRF-Token"] = csrf_of(other)
    assert "No videos yet" in other.get("/history").text
    for action in ("restore", "delete"):
        assert other.post(f"/generations/{gid}/{action}").status_code == 404
    assert other.get(f"/generations/{gid}/file?download=1").status_code == 404
    assert "a calm lake" not in other.get(f"/?prompt_from={gid}").text  # foreign prompt not leaked
    assert gen(app, gid) is not None


def test_download_sets_attachment_name(app, authed, drive_connected, net):
    net.worker.video_bytes = b"\x00\x00\x00\x18ftypmp42" + bytes(64)
    gid = submit(authed, prompt="boat / at: dusk").json()["id"]
    net.worker.jobs["job1"]["status"] = "done"
    tick(app)
    r = authed.get(f"/generations/{gid}/file?download=1")
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment; filename*=UTF-8''") and f"boat%20at%20dusk%20%28{gid}%29.mp4" in cd
    assert "content-disposition" not in authed.get(f"/generations/{gid}/file").headers


def test_use_prompt_again_prefills_studio(app, authed, user):
    with app.state.db.session() as s:
        g = Generation(user_id=user, prompt="a neon city in rain", aspect="16:9", ref_images=[],
                       status=GenStatus.READY, poll_errors=0, upload_attempts=0, dismissed=True)
        s.add(g)
        s.flush()
        gid = g.id
    html = authed.get(f"/?prompt_from={gid}").text
    assert ">a neon city in rain</textarea>" in html
    assert re.search(r'value="16:9" checked', html)
    assert authed.get("/?prompt_from=99999").status_code == 200


# ---- multi-scene videos -----------------------------------------------------------
LIMIT = {"kind": "limit_reached", "message": "Gemini's video limit has been reached."}


def submit_scenes(client, scenes, aspect="9:16"):
    return client.post("/generations", data={"scenes": scenes, "aspect": aspect})


def clip_bytes(tmp_path, name, seconds=1.0):
    from tests.fakes import make_clip
    return make_clip(tmp_path / f"{name}.mp4", seconds).read_bytes()


def duration_of(data: bytes, tmp_path) -> float:
    import subprocess
    path = tmp_path / "joined.mp4"
    path.write_bytes(data)
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out)


def finish(net, app, job):
    net.worker.jobs[job]["status"] = "done"
    tick(app)


def test_scenes_are_made_one_at_a_time_then_joined(app, authed, drive_connected, net, tmp_path):
    for job in ("job1", "job2", "job3"):
        net.worker.clips[job] = clip_bytes(tmp_path, job)
    r = submit_scenes(authed, ["a boat at dawn", "the boat at noon", "the boat at dusk"])
    assert r.status_code == 200
    gid = r.json()["id"]
    assert len(net.worker.submits) == 1  # only the first scene goes to the worker
    assert b"a boat at dawn" in net.worker.submits[0]["body"]

    finish(net, app, "job1")
    g = gen(app, gid)
    assert g.status == GenStatus.GENERATING and g.worker_job_id == "job2"
    assert b"the boat at noon" in net.worker.submits[1]["body"]
    card = authed.get("/api/generations").json()["cards"][0]["html"]
    assert "Generating scene 2 of 3" in card and "3 scenes" in card

    finish(net, app, "job2")
    finish(net, app, "job3")
    g = gen(app, gid)
    assert g.status == GenStatus.READY, g.error
    assert len(net.worker.submits) == 3
    stored = net.google.files[g.drive_file_id]
    assert 2.8 <= duration_of(stored["data"], tmp_path) <= 3.3
    assert re.fullmatch(r"\d{4}-\d\d-\d\d a boat at dawn \(\d+\)\.mp4", stored["name"])
    assert not any(Path(app.state.settings.generated_dir).iterdir())  # scene clips and joined copy cleaned up


def test_limit_mid_video_pauses_keeps_scenes_and_resumes(app, authed, drive_connected, net, tmp_path):
    net.worker.clips["job1"] = clip_bytes(tmp_path, "s1")
    net.worker.clips["job3"] = clip_bytes(tmp_path, "s2")
    gid = submit_scenes(authed, ["first", "second"]).json()["id"]
    finish(net, app, "job1")

    net.worker.paused = LIMIT
    net.worker.jobs["job2"].update(status="failed", error=LIMIT["message"])
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.PAUSED and "1 of 2 scenes are done" in g.error
    assert "Click Resume on this video" in g.error
    assert Path(g.scenes[0].clip_path).exists() and g.scenes[1].clip_path is None
    card = authed.get("/api/generations").json()["cards"][0]["html"]
    assert "1 of 2 scenes done" in card and f"/generations/{gid}/resume" in card

    net.worker.paused = None  # "Resume generation" in Settings only unpauses the worker…
    tick(app, 3)
    assert gen(app, gid).status == GenStatus.PAUSED and len(net.worker.submits) == 2  # …the video waits

    assert authed.post(f"/generations/{gid}/resume").status_code == 200  # the video's own Resume
    g = gen(app, gid)
    assert g.status == GenStatus.GENERATING and g.worker_job_id == "job3"
    assert b"second" in net.worker.submits[2]["body"]
    finish(net, app, "job3")
    assert gen(app, gid).status == GenStatus.READY


def test_submit_rejected_while_paused_pauses_instead_of_failing(app, authed, drive_connected, net):
    net.worker.paused = LIMIT
    net.worker.submit_response = (409, {"detail": LIMIT["message"]})
    gid = submit_scenes(authed, ["a", "b"]).json()["id"]
    g = gen(app, gid)
    assert g.status == GenStatus.PAUSED and "Click Resume on this video" in g.error
    assert authed.post(f"/generations/{gid}/cancel").status_code == 200
    assert gen(app, gid).status == GenStatus.CANCELLED


def test_failed_scene_is_named_and_retry_keeps_finished_ones(app, authed, drive_connected, net, tmp_path):
    net.worker.clips["job1"] = clip_bytes(tmp_path, "s1")
    net.worker.clips["job3"] = clip_bytes(tmp_path, "s2")
    gid = submit_scenes(authed, ["first", "second"]).json()["id"]
    finish(net, app, "job1")
    net.worker.jobs["job2"].update(status="failed", error="Gemini couldn't make this video.")
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.FAILED and g.error == "Scene 2: Gemini couldn't make this video."

    assert authed.post(f"/generations/{gid}/retry").status_code == 200
    assert len(net.worker.submits) == 3 and b"second" in net.worker.submits[2]["body"]  # scene 1 not redone
    finish(net, app, "job3")
    assert gen(app, gid).status == GenStatus.READY


@pytest.mark.parametrize("scenes,message", [
    (["x"] * 13, "at most 12 scenes"),
    (["fine", "  "], "Write scene 2 first."),
    (["fine", "y" * 2001], "Keep scene 2 under 2000 characters."),
])
def test_scene_validation(authed, drive_connected, scenes, message):
    r = submit_scenes(authed, scenes)
    assert r.status_code == 400 and message in r.json()["detail"]


def test_use_prompt_again_prefills_every_scene(app, authed, user):
    from app.models import Scene
    with app.state.db.session() as s:
        g = Generation(user_id=user, prompt="Scene 1: dawn\n\nScene 2: dusk", aspect="9:16", ref_images=[],
                       status=GenStatus.READY, poll_errors=0, upload_attempts=0, dismissed=False,
                       scenes=[Scene(position=1, prompt="dawn"), Scene(position=2, prompt="dusk")])
        s.add(g)
        s.flush()
        gid = g.id
    html = authed.get(f"/?prompt_from={gid}").text
    assert ">dawn</textarea>" in html and ">dusk</textarea>" in html


def test_migration_gives_existing_videos_one_scene(tmp_path):
    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    url = f"sqlite:///{tmp_path}/m.db"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "0002")
    engine = sa.create_engine(url)
    with engine.begin() as c:
        c.exec_driver_sql("INSERT INTO generations (prompt, aspect, ref_images, status, worker_job_id, poll_errors, "
                          "upload_attempts, dismissed, created_at, updated_at) VALUES ('old one', '9:16', '[]', "
                          "'GENERATING', 'job9', 0, 0, 0, '2026-01-01', '2026-01-01')")
    command.upgrade(cfg, "head")
    with engine.begin() as c:
        assert c.exec_driver_sql("SELECT generation_id, position, prompt, clip_path FROM scenes").all() == \
            [(1, 1, "old one", None)]


# ---- one prompt + length: Gemini plans the scenes -------------------------------------
def submit_idea(client, prompt="a day in the life of a lighthouse keeper", duration=30):
    return client.post("/generations", data={"prompt": prompt, "duration": str(duration), "aspect": "9:16"})


def test_one_prompt_gemini_plans_scenes_then_they_are_made(app, authed, drive_connected, net, tmp_path):
    for job in ("job2", "job3", "job4"):
        net.worker.clips[job] = clip_bytes(tmp_path, job)
    gid = submit_idea(authed).json()["id"]
    g = gen(app, gid)
    assert g.status == GenStatus.PLANNING and g.planned and len(g.scenes) == 3
    assert net.worker.submits[0]["path"] == "/plans" and b'"scenes":3' in net.worker.submits[0]["body"]
    assert "Gemini is writing 3 scenes" in authed.get("/api/generations").json()["cards"][0]["html"]

    net.worker.jobs["job1"].update(status="done", scenes=["dawn at the lighthouse", "noon storm", "night beam"])
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.GENERATING and [sc.prompt for sc in g.scenes] == \
        ["dawn at the lighthouse", "noon storm", "night beam"]
    assert net.worker.submits[1]["path"] == "/videos" and b"dawn at the lighthouse" in net.worker.submits[1]["body"]

    for job in ("job2", "job3", "job4"):
        finish(net, app, job)
    g = gen(app, gid)
    assert g.status == GenStatus.READY, g.error
    assert g.prompt == "a day in the life of a lighthouse keeper"
    stored = net.google.files[g.drive_file_id]
    assert "a day in the life of a lighthouse keeper" in stored["name"]
    assert 2.8 <= duration_of(stored["data"], tmp_path) <= 3.3


def test_ten_seconds_skips_planning(app, authed, drive_connected, net):
    gid = submit_idea(authed, duration=10).json()["id"]
    assert gen(app, gid).status == GenStatus.GENERATING and net.worker.submits[0]["path"] == "/videos"


@pytest.mark.parametrize("duration", [0, 15, 130, "abc"])
def test_length_validation(authed, drive_connected, duration):
    r = submit_idea(authed, duration=duration)
    assert r.status_code == 400 and "Duration must be" in r.json()["detail"]


@pytest.mark.parametrize("text,seconds", [
    ("a 30 second video of a fox", 30),
    ("a 30-second video", 30),
    ("make it 1 minute long", 60),
    ("1.5 minutes of rain", 90),
    ("a 1:30 video", 90),
    ("two minutes of waves", 120),
    ("half a minute", 30),
    ("5 minutes long", 120),
    ("25 seconds", 30),
    ("a 3 sec clip", 10),
    ("a 40 second video with a 5-second pause", 40),
    ("a cat in a garden", None),
    ("80s style neon, the 1990s, 9:16 portrait, at 4:30 pm", None),
])
def test_duration_from_prompt(text, seconds):
    assert duration_from_prompt(text) == seconds


def test_default_duration_comes_from_prompt(app, authed, drive_connected, net):
    gid = submit_idea(authed, prompt="a 30 second video of a lighthouse at night", duration="").json()["id"]
    assert len(gen(app, gid).scenes) == 3 and b'"scenes":3' in net.worker.submits[0]["body"]


def test_default_duration_without_length_is_ten_seconds(app, authed, drive_connected, net):
    gid = submit_idea(authed, prompt="a lighthouse at night", duration="").json()["id"]
    assert len(gen(app, gid).scenes) == 1 and net.worker.submits[0]["path"] == "/videos"


def test_picked_duration_beats_prompt(app, authed, drive_connected, net):
    gid = submit_idea(authed, prompt="a 1 minute video of a lighthouse", duration=20).json()["id"]
    assert len(gen(app, gid).scenes) == 2


def test_plan_failure_and_retry_plans_again(app, authed, drive_connected, net):
    gid = submit_idea(authed, duration=20).json()["id"]
    net.worker.jobs["job1"].update(status="failed", error="Gemini didn't reply with a list of scenes.")
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.FAILED and g.error.startswith("Gemini couldn't plan the scenes:")
    authed.post(f"/generations/{gid}/retry")
    assert gen(app, gid).status == GenStatus.PLANNING and net.worker.submits[1]["path"] == "/plans"


def test_too_few_planned_scenes_fails(app, authed, drive_connected, net):
    gid = submit_idea(authed, duration=30).json()["id"]
    net.worker.jobs["job1"].update(status="done", scenes=["only one"])
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.FAILED and "didn't write all the scenes" in g.error


def test_limit_while_planning_pauses_then_plans_after_resume(app, authed, drive_connected, net):
    gid = submit_idea(authed, duration=20).json()["id"]
    net.worker.paused = LIMIT
    net.worker.jobs["job1"].update(status="failed", error=LIMIT["message"])
    tick(app)
    assert gen(app, gid).status == GenStatus.PAUSED
    tick(app, 2)
    assert gen(app, gid).status == GenStatus.PAUSED and len(net.worker.submits) == 1  # never restarts by itself

    assert authed.post(f"/generations/{gid}/resume").status_code == 200  # also lifts the worker's pause
    assert net.worker.resumed == 1 and net.worker.paused is None
    assert gen(app, gid).status == GenStatus.PLANNING and net.worker.submits[1]["path"] == "/plans"


def test_only_paused_videos_can_be_resumed(app, authed, drive_connected, net):
    gid = submit_idea(authed, duration=20).json()["id"]
    r = authed.post(f"/generations/{gid}/resume")
    assert r.status_code == 400 and "Only paused videos" in r.json()["detail"]


def test_cancel_while_planning(app, authed, drive_connected, net):
    gid = submit_idea(authed).json()["id"]
    assert authed.post(f"/generations/{gid}/cancel").status_code == 200
    assert gen(app, gid).status == GenStatus.CANCELLED and net.worker.cancelled == ["job1"]


def test_use_prompt_again_for_planned_video_prefills_prompt_and_length(app, authed, drive_connected, net):
    gid = submit_idea(authed, prompt="a lighthouse story", duration=30).json()["id"]
    html = authed.get(f"/?prompt_from={gid}").text
    assert ">a lighthouse story</textarea>" in html
    assert re.search(r'<option value="30" selected>', html)


def test_long_prompt_allowed_for_planned_videos(app, authed, drive_connected, net):
    gid = submit_idea(authed, prompt="a long story " * 400, duration=60).json()["id"]  # ~5,200 characters
    assert gen(app, gid).status == GenStatus.PLANNING and len(gen(app, gid).scenes) == 6


def test_long_prompt_for_ten_seconds_is_condensed_by_gemini(app, authed, drive_connected, net):
    gid = submit_idea(authed, prompt="x" * 3000, duration=10).json()["id"]
    g = gen(app, gid)
    assert g.status == GenStatus.PLANNING and len(g.scenes) == 1
    assert net.worker.submits[0]["path"] == "/plans"


def test_prompt_over_ten_thousand_characters_rejected(authed, drive_connected):
    r = submit_idea(authed, prompt="x" * 10001, duration=60)
    assert r.status_code == 400 and "10,000 characters" in r.json()["detail"]
