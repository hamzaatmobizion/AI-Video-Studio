"""Scripts, characters, camera and voice: the prompt compiler, the character library and the review flow."""
import pytest

from app import script
from app.enums import GenStatus
from app.models import Character
from tests.fakes import PNG
from tests.test_app import clip_bytes, finish, gen, tick

ALI = {"id": 1, "name": "Ali", "appearance": "man in his 30s, short black hair, trimmed beard",
       "outfit": "white kurta", "voice": "deep, calm, slow", "personality": "warm", "image_path": "/refs/ali.png"}
SARA = {"id": 2, "name": "Sara", "appearance": "woman in her 20s, long curly hair", "outfit": "", "voice": "",
        "personality": "", "image_path": None}
CAST = [ALI, SARA]


# ---- compiler -----------------------------------------------------------------------------
def test_normalize_matches_cast_by_name_and_canonical_shot_words():
    sc = script.normalize_scene({
        "setting": "  a rooftop \n in Lahore ", "time_of_day": "dusk",
        "characters": [{"name": "ali", "position": "Frame Left"}, {"name": "Stranger"}, {"name": "ALI"}, 7],
        "shot": {"framing": "Close-up shot", "angle": "low", "movement": "slow push-in", "lens": "35mm"},
        "dialogue": [{"name": "Ali", "line": "Look up!"}, {"name": "Sara", "line": ""}],
        "unknown": "dropped"}, CAST)
    assert sc["setting"] == "a rooftop in Lahore"
    assert sc["characters"] == [{"id": 1, "name": "Ali", "position": "frame left"},
                                {"id": None, "name": "Stranger", "position": ""}]
    assert sc["shot"] == {"framing": "close-up", "angle": "low angle", "movement": "slow push-in", "lens": "35mm lens"}
    assert sc["dialogue"] == [{"id": 1, "name": "Ali", "line": "Look up!"}]
    assert "unknown" not in sc


def test_compile_repeats_library_words_camera_and_voice():
    sc = script.normalize_scene({
        "setting": "rooftop in old Lahore", "time_of_day": "golden hour",
        "characters": [{"id": 1, "position": "frame left"}, {"name": "Sara", "position": "frame right"}],
        "shot": {"framing": "medium", "angle": "eye level", "movement": "static", "lens": "50mm lens"},
        "action": "Ali hands Sara a red kite", "dialogue": [{"name": "Ali", "line": "Hold it tight."}],
        "sound": "wind, kite string humming"}, CAST)
    text = script.compile_prompt(sc, CAST, style="Cinematic film", language="Urdu")
    assert text.splitlines() == [
        "Style: Cinematic film.",
        "Setting: rooftop in old Lahore, golden hour.",
        "Camera: medium shot, eye level, static, 50mm lens.",
        "Characters: Ali (man in his 30s, short black hair, trimmed beard; white kurta), frame left. "
        "Sara (woman in her 20s, long curly hair), frame right.",
        "Ali hands Sara a red kite.",
        "Ali (voice: deep, calm, slow) says in Urdu: 'Hold it tight.'",
        "Sound: wind, kite string humming.",
        "Only the named characters speak, clearly and in sync with their lips. No subtitles.",
    ]


def test_plain_action_compiles_to_itself():
    assert script.compile_prompt(script.normalize_scene("a paper boat\nat dusk", []), []) == "a paper boat\nat dusk"


def test_warnings_for_long_lines_and_prompts():
    sc = script.normalize_scene({"dialogue": [{"name": "Ali", "line": "word " * 30}]}, CAST)
    assert "30 spoken words" in script.scene_warnings(sc, "short")[0]
    assert "shorten this scene" in script.scene_warnings({"dialogue": []}, "x" * 2001)[0]


def test_scene_images_puts_characters_in_shot_first():
    sc = {"characters": [{"id": 2, "name": "Sara"}, {"id": 1, "name": "Ali"}]}
    assert script.scene_images(sc, CAST, ["/u/1.png", "/u/2.png", "/u/3.png"]) == ["/refs/ali.png", "/u/1.png", "/u/2.png"]
    assert script.scene_images({"characters": []}, CAST, ["/u/1.png"]) == ["/u/1.png"]


# ---- character library ----------------------------------------------------------------------
def add_character(client, name="Ali", image=False, **fields):
    data = {"name": name, "appearance": "man in his 30s, beard", "outfit": "white kurta", "voice": "deep", **fields}
    files = [("image", ("ali.png", PNG, "image/png"))] if image else []
    return client.post("/characters", data=data, files=files)


def test_character_crud_and_image(app, authed):
    r = add_character(authed, image=True)
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert "Ali" in authed.get("/characters").text
    assert authed.get(f"/characters/{cid}/image").content == PNG
    assert add_character(authed, name="ali").status_code == 400  # names are unique per user
    assert add_character(authed, name="Bob", appearance="").status_code == 400
    r = authed.post(f"/characters/{cid}", data={"name": "Ali Khan", "appearance": "older now", "remove_image": "1"})
    assert r.status_code == 200
    with app.state.db.session() as s:
        ch = s.get(Character, cid)
        assert (ch.name, ch.appearance, ch.image_path) == ("Ali Khan", "older now", None)
    assert authed.get(f"/characters/{cid}/image").status_code == 404
    assert authed.post(f"/characters/{cid}/delete").status_code == 200
    with app.state.db.session() as s:
        assert s.get(Character, cid) is None


def test_characters_are_private(app, authed, user, drive_connected):
    with app.state.db.session() as s:
        s.add(Character(user_id=None, name="Theirs", appearance="x"))
        s.flush()
        other = s.query(Character).filter_by(name="Theirs").one().id
    assert authed.post(f"/characters/{other}/delete").status_code == 404
    assert authed.get(f"/characters/{other}/image").status_code == 404
    r = authed.post("/generations", data={"prompt": "x", "aspect": "9:16", "cast": [str(other)]})
    assert r.status_code == 400


# ---- review flow -------------------------------------------------------------------------------
def test_cast_and_review_flow(app, authed, drive_connected, net, tmp_path):
    ali = add_character(authed, image=True).json()["id"]
    r = authed.post("/generations", data={"prompt": "Ali flies a kite", "duration": "20", "aspect": "9:16",
                                          "cast": [str(ali)], "style": "Anime", "language": "Urdu", "review": "1"})
    gid = r.json()["id"]
    plan = net.worker.submits[0]
    assert plan["path"] == "/plans" and plan["json"]["cast"][0]["name"] == "Ali"
    assert plan["json"]["cast"][0]["voice"] == "deep" and plan["json"]["style"] == "Anime"

    net.worker.jobs["job1"].update(status="done", scenes=[
        {"setting": "rooftop", "characters": [{"name": "Ali", "position": "center"}], "shot": {"framing": "wide"},
         "action": "Ali runs with the kite", "dialogue": [{"name": "Ali", "line": "Chalo!"}]},
        {"setting": "rooftop", "action": "the kite climbs", "shot": {"framing": "low angle"}}])
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.REVIEW and [sc.prompt for sc in g.scenes] == ["", ""]
    assert g.scenes[0].characters == [{"id": ali, "name": "Ali", "position": "center"}]
    tick(app, 2)
    assert len(net.worker.submits) == 1  # nothing is made until the script is approved
    card = authed.get("/api/generations").json()["cards"][0]["html"]
    assert "Script ready to review" in card and f"/generations/{gid}/script" in card
    page = authed.get(f"/generations/{gid}/script").text
    assert "Approve &amp; generate" in page and "Chalo!" in page

    edited = [script.normalize_scene(sc.script(), []) for sc in g.scenes]
    edited[0]["shot"]["framing"] = "close-up"
    edited[1]["characters"] = [{"id": ali, "position": "frame left"}]
    r = authed.post(f"/generations/{gid}/script", json={"scenes": edited, "style": "Anime", "language": "Urdu"})
    assert r.status_code == 200
    prompts = [s["prompt"] for s in r.json()["scenes"]]
    assert "Camera: close-up shot." in prompts[0] and "Ali (voice: deep) says in Urdu: 'Chalo!'" in prompts[0]
    assert "Ali (man in his 30s, beard; white kurta), frame left." in prompts[1]
    assert gen(app, gid).status == GenStatus.REVIEW

    for job in ("job2", "job3"):
        net.worker.clips[job] = clip_bytes(tmp_path, job)
    r = authed.post(f"/generations/{gid}/script", json={"scenes": edited, "style": "Anime", "language": "Urdu",
                                                         "approve": True})
    assert r.status_code == 200
    g = gen(app, gid)
    assert g.status == GenStatus.GENERATING and g.scenes[0].prompt == prompts[0]
    clip = net.worker.submits[1]
    assert clip["path"] == "/videos" and clip["multipart"] and clip["images"] == 1  # Ali's picture
    finish(net, app, "job2")
    finish(net, app, "job3")
    assert gen(app, gid).status == GenStatus.READY
    r = authed.post(f"/generations/{gid}/script", json={"scenes": edited})
    assert r.status_code == 400 and "can't be changed" in r.json()["detail"]


def test_approve_refuses_empty_or_too_long_scenes(app, authed, drive_connected, net):
    gid = authed.post("/generations", data={"scenes": ["a boat"], "aspect": "9:16", "review": "1"}).json()["id"]
    assert gen(app, gid).status == GenStatus.REVIEW and not net.worker.submits  # own scenes: no Gemini needed
    r = authed.post(f"/generations/{gid}/script", json={"scenes": [{"action": ""}], "approve": True})
    assert r.status_code == 400 and "Scene 1 is empty" in r.json()["detail"]
    r = authed.post(f"/generations/{gid}/script", json={"scenes": [{"action": "x" * 1500, "setting": "y" * 400,
                                                                     "sound": "z" * 300}],
                                                         "approve": True})
    assert r.status_code == 400 and "shorten it" in r.json()["detail"]
    assert gen(app, gid).status == GenStatus.REVIEW


def test_editor_can_add_and_remove_scenes(app, authed, drive_connected, net):
    gid = authed.post("/generations", data={"scenes": ["a", "b"], "aspect": "9:16", "review": "1"}).json()["id"]
    authed.post(f"/generations/{gid}/script", json={"scenes": [{"action": "one"}, {"action": "two"}, {"action": "3"}]})
    assert [s.action for s in gen(app, gid).scenes] == ["one", "two", "3"]
    authed.post(f"/generations/{gid}/script", json={"scenes": [{"action": "only"}]})
    assert [(s.position, s.action) for s in gen(app, gid).scenes] == [(1, "only")]
    assert authed.post(f"/generations/{gid}/script", json={"scenes": []}).status_code == 400


def test_cancel_in_review_then_retry_returns_to_review(app, authed, drive_connected, net):
    gid = authed.post("/generations", data={"scenes": ["a boat"], "aspect": "9:16", "review": "1"}).json()["id"]
    assert authed.post(f"/generations/{gid}/cancel").status_code == 200
    assert gen(app, gid).status == GenStatus.CANCELLED
    authed.post(f"/generations/{gid}/retry")
    g = gen(app, gid)
    assert g.status == GenStatus.REVIEW and g.scenes[0].action == "a boat" and not net.worker.submits


def test_style_without_review_is_compiled_straight_away(app, authed, drive_connected, net):
    authed.post("/generations", data={"prompt": "a paper boat", "aspect": "9:16", "style": "Pixar-style 3D"})
    assert net.worker.submits[0]["path"] == "/videos"
    assert b"Style: Pixar-style 3D." in net.worker.submits[0]["body"]


def test_planned_without_review_compiles_and_starts(app, authed, drive_connected, net):
    authed.post("/generations", data={"prompt": "kites", "duration": "10", "aspect": "9:16",
                                      "cast": [str(add_character(authed).json()["id"])]})
    assert net.worker.submits[0]["path"] == "/plans"  # a cast always gets a script
    net.worker.jobs["job1"].update(status="done", scenes=[{"characters": [{"name": "Ali"}], "action": "Ali waves"}])
    tick(app)
    assert net.worker.submits[1]["path"] == "/videos"
    assert b"Characters: Ali (man in his 30s, beard; white kurta)." in net.worker.submits[1]["body"]


def test_studio_shows_cast_and_direction(authed, drive_connected):
    add_character(authed)
    html = authed.get("/").text
    assert 'name="cast"' in html and "Ali" in html and 'name="style"' in html and 'name="review"' in html


def test_old_database_gets_script_columns(tmp_path):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect
    url = f"sqlite:///{tmp_path}/old.db"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "0004")
    engine = create_engine(url)
    with engine.begin() as c:
        c.exec_driver_sql("INSERT INTO generations (prompt, aspect, ref_images, status, poll_errors, upload_attempts, "
                          "dismissed, planned, created_at, updated_at) VALUES ('old', '9:16', '[]', 'READY', 0, 0, 0, "
                          "0, '2026-01-01', '2026-01-01')")
        c.exec_driver_sql("INSERT INTO scenes (generation_id, position, prompt) VALUES (1, 1, 'old')")
    command.upgrade(cfg, "head")
    cols = {c["name"] for c in inspect(engine).get_columns("scenes")}
    assert {"setting", "shot", "characters", "dialogue"} <= cols
    with engine.begin() as c:
        assert c.exec_driver_sql("SELECT cast_ids, style, review FROM generations").one() == ("[]", "", 0)
        assert c.exec_driver_sql("SELECT action, shot, dialogue FROM scenes").one() == ("", "{}", "[]")
