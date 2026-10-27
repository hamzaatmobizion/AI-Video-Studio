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
    text = script.compile_prompt(sc, CAST, style="Cinematic film", language="Urdu", aspect="16:9")
    assert text.splitlines() == [
        "Widescreen 16:9 video.",
        "Cinematic film.",
        "Rooftop in old Lahore, golden hour.",
        "Medium shot, eye level, static, 50mm lens.",
        "Characters: Ali, frame left, exactly as in reference image 1 (same face, hair, outfit and style). "
        "Sara (woman in her 20s, long curly hair), frame right.",
        "Ali hands Sara a red kite.",
        "Ali (voice: deep, calm, slow) says in Urdu: 'Hold it tight.'",
        "Sound: Wind, kite string humming.",
        "Only the named characters speak, clearly and in sync with their lips. " + script.SPEAKERS,
        " ".join([script.ONE_SHOT, script.CONTINUITY, script.REALISM, script.NO_TEXT]),
    ]


def test_turning_scene_says_how_the_body_turns_and_frees_the_face():
    sc = script.normalize_scene({"setting": "a courtyard", "characters": [{"id": 1}],
                                 "action": "Ali spins in a slow circle"}, CAST)
    text = script.compile_prompt(sc, CAST)
    assert "Ali: same person, hair, outfit and style as reference image 1; the picture only shows them" in text
    assert "exactly as in reference image" not in text
    assert text.splitlines()[-2] == script.TURN_NOTE
    calm = script.compile_prompt(script.normalize_scene({"setting": "a courtyard", "characters": [{"id": 1}],
                                                         "action": "Ali sits and talks"}, CAST), CAST)
    assert "exactly as in reference image 1 (same face" in calm and script.TURN_NOTE not in calm
    assert script.compile_prompt({"action": "a woman dancing"}, []).splitlines()[:2] == ["a woman dancing",
                                                                                         script.TURN_NOTE]


def test_style_comes_from_the_prompt_when_the_field_is_empty():
    idea = ("Create a 20-second 3D animated cartoon video.\n\nVisual Style:\nSoft light.\n\n"
            "Technical Specifications:\n- Style: 3D animated cartoon, cheerful, high detail\n")
    assert script.style_from_prompt(idea) == "3D animated cartoon, cheerful, high detail"
    assert script.style_from_prompt("A 10-second 3D animated cartoon of friends, cinematic") == \
        "3D animated cartoon, cinematic"
    assert script.style_from_prompt("a paper boat at dusk") == ""


def test_a_cartoon_named_in_the_scene_gets_the_cartoon_rule():
    text = script.compile_prompt({"setting": "3D animated cartoon garden", "action": "a fox naps"}, [])
    assert script.ANIMATED_REALISM in text and script.REALISM not in text


def test_per_shot():
    assert (script.per_shot(2, 1), script.per_shot(6, 2), script.per_shot(6, 1), script.per_shot(8, 1)) == (3, 3, 6, 6)


def test_invented_baby_is_never_taken_for_a_picked_character_and_keeps_its_look():
    kid = {"id": 9, "name": "child", "appearance": "boy aged 12, school bag", "outfit": "", "voice": "",
           "personality": "", "image_path": "/refs/kid.png"}
    bible = {"characters": [{"name": "Baby", "look": "newborn infant in a soft blanket", "never": "standing"}]}
    sc = script.normalize_scene({"characters": [{"name": "Baby"}], "action": "Baby yawns"}, [kid])
    sc = script.ensure_cast(sc, [kid], bible)
    assert sc["characters"] == [{"id": None, "name": "Baby", "position": ""}]  # not renamed to "child"
    named = script.ensure_cast(script.normalize_scene({"action": "Mother rocks Baby"}, []), [], bible)
    assert named["characters"] == [{"id": None, "name": "Baby", "position": ""}]  # named in the action: in shot
    p = script.compile_prompt(named, [], bible=bible)
    assert "Baby (newborn infant in a soft blanket)." in p and "Avoid: standing." in p
    assert script.scene_images(named, [kid], []) == []  # the 12-year-old's picture doesn't go with the baby


def test_compile_joins_setting_without_doubled_punctuation():
    sc = script.normalize_scene({"setting": "a snowy clearing.", "time_of_day": "day"}, [])
    assert script.compile_prompt(sc, []).splitlines()[0] == "A snowy clearing, day."


def test_ensure_cast_takes_a_renamed_character_to_be_the_chosen_one():
    sc = script.normalize_scene({"characters": [{"name": "Young Man", "position": "center"}],
                                 "dialogue": [{"name": "Young Man", "line": "Brr!"}]}, [ALI])
    sc = script.ensure_cast(sc, [ALI])
    assert sc["characters"] == [{"id": 1, "name": "Ali", "position": "center"}]
    assert sc["dialogue"] == [{"id": 1, "name": "Ali", "line": "Brr!"}]
    assert script.scene_images(sc, [ALI], []) == ["/refs/ali.png"]


def test_ensure_cast_adds_a_chosen_character_named_in_the_action():
    sc = script.ensure_cast(script.normalize_scene({"action": "Sara laughs as ali trips"}, CAST), CAST)
    assert [p["id"] for p in sc["characters"]] == [1, 2]


def test_ensure_cast_leaves_shots_without_the_cast_alone():
    sc = script.ensure_cast(script.normalize_scene({"action": "the kite climbs"}, CAST), CAST)
    assert sc["characters"] == []
    two = script.normalize_scene({"characters": [{"name": "Stranger"}, {"name": "Guard"}]}, [ALI])
    assert [p["id"] for p in script.ensure_cast(two, [ALI])["characters"]] == [None, None]  # ambiguous


def test_plain_action_keeps_its_words_and_gets_format_and_rules():
    text = script.compile_prompt(script.normalize_scene("a paper boat\nat dusk", []), [], aspect="9:16")
    assert text == "\n".join(["Vertical 9:16 video.", "a paper boat", "at dusk",
                              script.quality_rules({"action": "a paper boat"})])


def test_rules_fit_the_style_and_any_text_on_screen():
    cartoon = script.compile_prompt({"action": "a fox naps"}, [], style="Pixar-style 3D animation")
    assert script.ANIMATED_REALISM in cartoon and script.REALISM not in cartoon
    sign = script.compile_prompt({"action": "a shop sign reads 'Chai Wala'"}, [])
    assert script.NO_TEXT not in sign
    talk = script.compile_prompt(script.normalize_scene({"action": "a sign above the door", "dialogue": [
        {"name": "Ali", "line": "Hi"}]}, CAST), CAST)
    assert "in sync with their lips. No subtitles." in talk  # no blanket no-text rule, so still no subtitles


def test_fit_shortens_the_action_and_keeps_the_rules():
    sc = script.normalize_scene({"setting": "a beach", "action": "waves roll in " * 200, "sound": "surf " * 60}, [])
    full = script.compile_prompt(sc, [], aspect="9:16")
    fitted = script.compile_prompt(sc, [], aspect="9:16", fit=True)
    assert len(full) > script.MAX_PROMPT >= len(fitted)
    assert fitted.endswith(script.NO_TEXT) and fitted.startswith("Vertical 9:16 video.\nA beach.\nWaves roll in")


def test_warnings_for_long_lines_and_prompts():
    sc = script.normalize_scene({"dialogue": [{"name": "Ali", "line": "word " * 30}]}, CAST)
    assert "30 spoken words" in script.scene_warnings(sc, "short")[0]
    assert "is cut when it's sent" in script.scene_warnings({"dialogue": []}, "x" * 2001)[0]
    pictured = [{"id": i, "image_path": f"/refs/{i}.png"} for i in range(1, 5)]
    crowd = {"characters": [{"id": i} for i in range(1, 5)]}
    assert "4 pictured characters" in script.scene_warnings(crowd, "short", pictured)[0]
    assert script.scene_warnings({"characters": crowd["characters"][:3]}, "short", pictured) == []


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


def test_planning_uses_the_prompt_style_and_only_the_cast_given_a_role(app, authed, drive_connected, net):
    ids = [add_character(authed, name=n, image=True).json()["id"] for n in ("Ali", "Sara", "Omar", "Zoya")]
    r = authed.post("/generations", data={"prompt": "Friends chase a kite.\n- Style: Anime, bright", "duration": "20",
                                          "aspect": "9:16", "cast": [str(i) for i in ids]})
    gid = r.json()["id"]
    assert net.worker.submits[0]["json"]["style"] == "Anime, bright"
    net.worker.jobs["job1"].update(status="done", scenes=[
        {"setting": "park", "characters": [{"name": "Ali"}], "action": "Ali runs"},
        {"setting": "park", "characters": [{"name": "Sara"}], "action": "Sara laughs"}])
    tick(app)
    g = gen(app, gid)
    # Omar and Zoya got no role: they're left out, not forced into a shot where Veo would take them for someone else
    assert [[p["name"] for p in sc.characters] for sc in g.scenes] == [["Ali"], ["Sara"]]
    assert g.scenes[0].prompt.startswith("Vertical 9:16 video.\nAnime, bright.")
    assert script.ANIMATED_REALISM in g.scenes[0].prompt


def test_a_video_takes_at_most_eight_characters(app, authed, drive_connected):
    ids = [add_character(authed, name=f"P{i}").json()["id"] for i in range(9)]
    r = authed.post("/generations", data={"prompt": "a party", "duration": "10", "aspect": "9:16", "cast": ids})
    assert r.status_code == 400 and "at most 8 characters" in r.json()["detail"]
    assert 'data-max="8"' in authed.get("/").text


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
    assert g.scenes[1].characters == []  # Ali is already in scene 1, so nobody is added
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
    assert "Close-up shot." in prompts[0] and "Ali (voice: deep) says in Urdu: 'Chalo!'" in prompts[0]
    # the picture carries a pictured character's look; their saved words aren't sent with it
    assert "Ali, frame left, exactly as in reference image 1" in prompts[1] and "white kurta" not in prompts[1]
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
    assert b"Pixar-style 3D." in net.worker.submits[0]["body"]


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
    assert 'name="cast"' in html and "Ali" in html and 'name="review"' in html
    # video type, style and language come from the prompt (and can be changed on the script page)
    assert 'name="style"' not in html and 'name="genre"' not in html and 'name="language"' not in html


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


# ---- AI portraits --------------------------------------------------------------------------
def test_portrait_flow_to_character_picture(app, authed, net):
    r = authed.post("/portraits", json={"appearance": "man in his 30s, beard", "outfit": "kurta", "style": "Anime"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    sent = net.worker.submits[0]
    assert sent["path"] == "/portraits" and sent["json"]["description"] == "man in his 30s, beard. Outfit: kurta"
    assert sent["json"]["style"] == "Anime"
    assert authed.get(f"/portraits/{pid}").json()["status"] == "working"
    r = authed.post("/portraits", json={"appearance": "x"})
    assert r.status_code == 409 and "still drawing" in r.json()["detail"]  # one at a time

    net.worker.jobs["job1"].update(status="done", image=True)
    net.worker.clips["job1"] = PNG
    st = authed.get(f"/portraits/{pid}").json()
    assert st["status"] == "ready" and st["image_url"] == f"/portraits/{pid}/image"
    assert authed.get(st["image_url"]).content == PNG

    cid = add_character(authed, portrait_id=str(pid)).json()["id"]
    with app.state.db.session() as s:
        ch = s.get(Character, cid)
        assert ch.image_path and ch.image_url.startswith(f"/characters/{cid}/image?v=")
    assert authed.get(f"/characters/{cid}/image").content == PNG


def test_portrait_failure_and_privacy(app, authed, net):
    pid = authed.post("/portraits", json={"appearance": "a man"}).json()["id"]
    net.worker.jobs["job1"].update(status="failed", error="Gemini declined this description.")
    st = authed.get(f"/portraits/{pid}").json()
    assert st["status"] == "failed" and st["error"] == "Gemini declined this description."
    assert add_character(authed, portrait_id=str(pid)).status_code == 400  # not a usable picture
    assert authed.post("/portraits", json={"appearance": ""}).status_code == 400
    with app.state.db.session() as s:
        from app.models import Portrait
        s.add(Portrait(user_id=None, job_id="x", status="ready", image_path="/etc/hosts"))
        s.flush()
        other = s.query(Portrait).filter_by(job_id="x").one().id
    assert authed.get(f"/portraits/{other}").status_code == 404
    assert authed.get(f"/portraits/{other}/image").status_code == 404
    assert add_character(authed, name="Zed", portrait_id=str(other)).status_code == 400


def test_characters_page_has_portrait_studio(authed):
    html = authed.get("/characters").text
    assert 'id="cp-draw"' in html and 'id="cp-frame"' in html and "Build your cast" in html
    add_character(authed)
    html = authed.get("/characters").text
    assert 'class="cp-card"' in html and "1 of 50" in html


# ---- video types and the story bible ------------------------------------------------------
@pytest.mark.parametrize("prompt, genre", [
    ("A preschool video teaching the alphabet: A is for apple", "kids"),
    ("30-second commercial for our new running shoe", "ad"),
    ("How to make chai, step by step", "explainer"),
    ("A short film about a lost kite", "story"),
    ("A paper boat drifting down a street", "general"),
    # a comedy that asks for a dhol beat underneath is not a music video
    ("Punjabi village comedy. Background music: funny dhol beat. Ending music: fast dhol beat.", "comedy"),
    ("A music video: she sings and dances on a rooftop", "music"),
])
def test_genre_from_prompt(prompt, genre):
    from app.genres import genre_from_prompt
    assert genre_from_prompt(prompt) == genre


def test_plan_rules_spread_the_arc_over_the_scenes():
    from app.genres import plan_rules, valid_genre
    rules = plan_rules("kids", 4)
    assert "scene 1 = Hook, scene 2 = Learn, scene 3 = Repeat, scene 4 = Celebrate" in rules
    assert "nothing scary" in rules and rules.startswith("- This is a kids / educational video.")
    assert "scene 1 = Hook" in plan_rules("ad", 6) and "scene 6 = Call to action" in plan_rules("ad", 6)
    assert "(all in one scene)" in plan_rules("nope", 1)  # unknown: general
    assert valid_genre("AD", "kids alphabet") == "ad" and valid_genre("", "kids alphabet") == "kids"


def test_normalize_bible_keeps_known_bounded_fields():
    b = script.normalize_bible({"title": " Fiona ", "palette": "greens, sky blue", "extra": 1, "characters": [
        {"name": "Fiona", "look": "cream bunny, ears down", "never": "upright ears"}, {"name": "fiona", "look": "x"},
        {"name": "Empty"}, "junk"]})
    assert b["title"] == "Fiona" and "extra" not in b and b["style"] == ""
    assert b["characters"] == [{"name": "Fiona", "look": "cream bunny, ears down", "never": "upright ears", "voice": ""}]
    assert script.normalize_bible(None)["characters"] == []


def test_compile_uses_bible_locks_and_avoid_list():
    bible = {"style": "Pixar-style 3D", "palette": "bright greens, apple red",
             "characters": [{"name": "Fiona", "look": "cream bunny, floppy ears hanging down", "never": "upright ears"}]}
    locked = {**ALI, "locked": "trimmed beard, white kurta", "never": "glasses; hat"}
    sc = {"characters": [{"id": None, "name": "Fiona", "position": "center"}, {"id": 1, "name": "Ali", "position": ""}],
          "action": "Fiona waves at Ali", "dialogue": [], "shot": {}}
    p = script.compile_prompt(sc, [locked], aspect="9:16", bible=bible, genre="kids", music=False)
    lines = p.splitlines()
    assert lines[:3] == ["Vertical 9:16 video.", "Pixar-style 3D.", "Colour palette: bright greens, apple red."]
    assert "Fiona (cream bunny, floppy ears hanging down), center." in p
    assert "Ali, exactly as in reference image 1 (same face, hair, outfit and style); always trimmed beard, " \
           "white kurta." in p
    assert "Avoid: scary faces, sharp teeth, danger, violence, upright ears, glasses, hat." in p
    assert p.endswith(script.NO_MUSIC) and script.ANIMATED_REALISM in p
    assert script.NO_MUSIC not in script.compile_prompt(sc, [locked], bible=bible)  # one clip keeps its music
    sung = {**sc, "sound": "she sings a song"}
    assert script.NO_MUSIC not in script.compile_prompt(sung, [], music=False)  # asked for in the scene


def test_planned_video_gets_type_rules_and_bible(app, authed, drive_connected, net):
    gid = authed.post("/generations", data={"prompt": "A preschool alphabet video: A is for apple", "duration": "20",
                                            "aspect": "9:16"}).json()["id"]
    body = net.worker.submits[0]["json"]
    assert gen(app, gid).genre == "kids" and "scene 1 = Hook, scene 2 = Celebrate" in body["rules"]
    net.worker.jobs["job1"].update(status="done", bible={"title": "Fiona", "style": "Pixar-style 3D",
                                                         "palette": "greens", "characters": [
                                                             {"name": "Fiona", "look": "cream bunny", "never": "upright ears"}]},
                                   scenes=[{"characters": [{"name": "Fiona"}], "action": "Fiona hops in"},
                                           {"characters": [{"name": "Fiona"}], "action": "Fiona waves bye"}])
    tick(app)
    g = gen(app, gid)
    assert g.bible["title"] == "Fiona" and g.style == "Pixar-style 3D"
    assert all("Fiona (cream bunny)." in s.prompt and "upright ears" in s.prompt and "Colour palette: greens." in s.prompt
               for s in g.scenes)
    assert b"Avoid: scary faces" in net.worker.submits[1]["body"]


def test_review_edits_bible_and_type(app, authed, drive_connected, net):
    gid = authed.post("/generations", data={"scenes": ["a boat"], "aspect": "9:16", "review": "1"}).json()["id"]
    r = authed.post(f"/generations/{gid}/script", json={"scenes": [{"action": "a boat"}], "genre": "ad",
                                                         "bible": {"palette": "teal and orange"}})
    assert r.status_code == 200 and "Colour palette: teal and orange." in r.json()["scenes"][0]["prompt"]
    g = gen(app, gid)
    assert g.genre == "ad" and g.bible["palette"] == "teal and orange"
    html = authed.get(f"/generations/{gid}/script").text
    assert 'id="bible-palette"' in html and "Ad / product" in html


def test_character_locked_traits_saved(app, authed):
    r = authed.post("/characters", data={"name": "Fiona", "appearance": "cream bunny", "locked": "ears hanging down",
                                         "never": "upright ears"})
    assert r.status_code == 200
    with app.state.db.session() as s:
        c = s.get(Character, r.json()["id"])
        assert (c.locked, c.never) == ("ears hanging down", "upright ears")
        assert script.cast_sheet(c.sheet())["appearance"] == "cream bunny; always ears hanging down"
    assert "Must never change" in authed.get("/characters").text


@pytest.mark.parametrize("prompt, aspect", [
    ("Format: 9:16 vertical, funny 3D animation", "9:16"),
    ("A widescreen 16:9 trailer", "16:9"),
    ("vertical reel, but crop to 16:9 later", "16:9"),  # a written ratio beats a word
    ("A horizontal shot of the sea", "16:9"),
    ("A portrait of Ali in a landscape of hills", ""),  # a picture and scenery, not a format
])
def test_aspect_from_prompt(prompt, aspect):
    assert script.aspect_from_prompt(prompt) == aspect


def test_prompt_aspect_wins_over_the_picked_one(app, authed, drive_connected, net):
    gid = authed.post("/generations", data={"prompt": "A 16:9 shot of the sea", "aspect": "9:16"}).json()["id"]
    assert gen(app, gid).aspect == "16:9" and b"Widescreen 16:9 video." in net.worker.submits[0]["body"]
    gid = authed.post("/generations", data={"prompt": "A paper boat", "aspect": "16:9"}).json()["id"]
    assert gen(app, gid).aspect == "16:9"


def test_style_from_prompt_drops_realistic_from_an_animated_look():
    assert script.style_from_prompt("Vertical 9:16, high-quality realistic 3D animation, comedy") == "3D animation"
    assert script.style_from_prompt("A realistic documentary about bees") == "realistic, documentary"
    assert script.style_from_prompt("Style: realistic 3D animation") == "realistic 3D animation"  # their own words


def test_real_people_refusal_names_the_pictured_characters(app, authed, drive_connected, net):
    cid = add_character(authed, image=True).json()["id"]
    gid = authed.post("/generations", data={"prompt": "Ali waves", "duration": "10", "aspect": "9:16",
                                            "cast": [str(cid)]}).json()["id"]
    net.worker.jobs["job1"].update(status="done", scenes=[{"characters": [{"name": "Ali"}], "action": "Ali waves"}])
    tick(app)
    net.worker.jobs["job2"].update(status="failed", error="Gemini won't make this clip because it takes the people "
                                                          "in the reference pictures for real people.")
    tick(app)
    assert gen(app, gid).error.endswith("Pictures sent with this scene: Ali.")


# ---- the prompt's own timeline, when Gemini's planner fails --------------------------------
TIMED = ("Setting: A cozy kitchen at night.\n0–5 seconds: Ali walks in and says: \"Hello!\"\n"
         "5–10 seconds: Sara waves.\n10–15 seconds: Ali laughs.\n15–20 seconds: They hug. 🎵 Music: dhol beat\n"
         "Visual style: 3D cartoon, bright.\nStrict rules: exactly 20 seconds.")


def test_script_from_timeline_splits_into_ten_second_scenes():
    from app.timeline import script_from_timeline
    scenes = script_from_timeline(TIMED, 2, [{"name": "Ali"}, {"name": "Sara"}, {"name": "Omar"}])
    assert [s["action"] for s in scenes] == ["Ali walks in and says. Then Sara waves.", "Ali laughs. Then They hug."]
    assert scenes[0]["dialogue"] == [{"name": "Ali", "line": "Hello!"}]  # a spoken line, not words in the action
    assert scenes[0]["setting"] == "A cozy kitchen at night."
    assert [[c["name"] for c in s["characters"]] for s in scenes] == [["Ali", "Sara"], ["Ali"]]
    numbered = "Scene 1: A boat sails.\nScene 2: It docks.\nScene 3: Gulls fly.\nScene 4: Night falls."
    assert [s["action"] for s in script_from_timeline(numbered, 2, [])] == ["A boat sails. Then It docks.",
                                                                           "Gulls fly. Then Night falls."]
    assert script_from_timeline("A paper boat on a rainy street.", 2, []) is None  # no timeline
    assert script_from_timeline("0–5 seconds: a boat.", 2, []) is None  # scene 2 would be empty


def test_timeline_shorter_than_the_flow_scenes_is_spread_over_them():
    """A 20-second timeline in three 8-second Flow scenes (24 s): nothing starts after 16 s, so by time the last
    scene would be empty; its moments are spread in order instead."""
    from app.timeline import script_from_timeline
    scenes = script_from_timeline(TIMED, 3, [], clip_seconds=8)
    assert [s["action"] for s in scenes] == ["Ali walks in and says. Then Sara waves.", "Ali laughs.", "They hug."]
    assert scenes[0]["dialogue"] == [{"name": "Ali", "line": "Hello!"}]
    assert script_from_timeline(TIMED, 5, [], clip_seconds=8) is None  # 4 moments can't fill 5 scenes


def test_failed_plan_falls_back_to_the_prompts_timeline(app, authed, drive_connected, net):
    gid = authed.post("/generations", data={"prompt": TIMED, "duration": "20", "aspect": "9:16"}).json()["id"]
    net.worker.jobs["job1"].update(status="failed", error="Gemini's chat answered with an error.")
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.GENERATING and g.scenes[1].action == "Ali laughs. Then They hug."
    assert net.worker.submits[1]["path"] == "/videos" and "A cozy kitchen at night" in g.scenes[0].prompt


def test_a_refused_plan_is_not_worked_around(app, authed, drive_connected, net):
    gid = authed.post("/generations", data={"prompt": TIMED, "duration": "20", "aspect": "9:16"}).json()["id"]
    net.worker.jobs["job1"].update(status="failed", error="Gemini declined this prompt for safety reasons.")
    tick(app)
    assert gen(app, gid).status == GenStatus.FAILED


# ---- checks before generating ---------------------------------------------------------------
def test_prompt_checks():
    from app.checks import prompt_checks
    urdu_voice = {"name": "Female", "voice": "female, Urdu accent, calm", "image_path": "/refs/f.png"}
    notes = prompt_checks('Pakistani Punjabi comedy, not Hindi or Urdu. She says: "ਨੀ ਨੂੰਹੇ!"', [urdu_voice])
    assert any("Gurmukhi" in n for n in notes) and not any("saved voice" in n for n in notes)  # the prompt's voice wins
    assert not any("Devanagari" in n for n in prompt_checks('Pakistani lullaby: "ਹੁਣ ਸੌਂ ਜਾ।"', []))  # । is Gurmukhi's too
    assert any("realistic look" in n for n in prompt_checks("A funny skit", [urdu_voice], style="realistic, 3D animation"))
    assert prompt_checks("high-quality realistic 3D animation, Urdu dialogue", [urdu_voice]) == []  # all fine
    # "Do NOT use Indian Punjabi … authentic Pakistani Punjabi": Punjabi is still asked for
    punjabi = {"name": "Mother", "voice": "warm, Punjabi accent"}
    assert prompt_checks("Do NOT use Indian Punjabi. Authentic Pakistani Punjabi.", [punjabi]) == []


def test_prompt_check_endpoint(authed):
    cid = add_character(authed, voice="Urdu accent").json()["id"]
    r = authed.post("/api/prompt-check", json={"prompt": 'Pakistani Punjabi: "ਨੀ ਨੂੰਹੇ!"', "cast": [cid]})
    assert r.status_code == 200 and r.json()["notes"][0]["level"] == "warning"
    assert "Gurmukhi" in r.json()["notes"][0]["text"]
    from app.checks import prompt_notes
    story = "Pappu, a chubby young boy, blows a kiss at his phone."
    assert [n["level"] for n in prompt_notes(story, [])] == ["error"]  # Gemini will likely refuse it
    assert 'id="prompt-notes"' in authed.get("/").text


def test_script_page_names_picked_characters_left_out(app, authed, drive_connected, net):
    ids = [add_character(authed, name=n).json()["id"] for n in ("Ali", "Omar")]
    gid = authed.post("/generations", data={"prompt": "Ali flies a kite", "duration": "10", "aspect": "9:16",
                                            "review": "1", "cast": [str(i) for i in ids]}).json()["id"]
    net.worker.jobs["job1"].update(status="done", scenes=[{"characters": [{"name": "Ali"}], "action": "Ali runs"}])
    tick(app)
    assert "Not in any scene: Omar." in authed.get(f"/generations/{gid}/script").text


def test_next_scene_continues_from_the_last_frame(app, authed, drive_connected, net, tmp_path):
    cid = add_character(authed, image=True).json()["id"]
    net.worker.clips["job2"] = clip_bytes(tmp_path, "job2")
    gid = authed.post("/generations", data={"prompt": "Ali flies a kite", "duration": "20", "aspect": "9:16",
                                            "cast": [str(cid)]}).json()["id"]
    net.worker.jobs["job1"].update(status="done", scenes=[{"characters": [{"name": "Ali"}], "action": "Ali runs"},
                                                          {"characters": [{"name": "Ali"}], "action": "Ali waves"}])
    tick(app)
    first = net.worker.submits[1]
    assert first["images"] == 1 and b"last frame of the previous shot" not in first["content"]
    finish(net, app, "job2")
    second = net.worker.submits[2]
    assert second["path"] == "/videos" and second["images"] == 2  # Ali's picture, then scene 1's last frame
    assert script.CONTINUE_FROM.format(n=2).encode() in second["content"]
    assert "last frame" not in gen(app, gid).scenes[1].prompt  # added when sent, not stored in the script


def test_prompt_checks_flag_an_outfit_without_colours():
    from app.checks import prompt_checks
    assert any("Saas: no picture, and the saved outfit names no colours" in n
               for n in prompt_checks("a comedy", [{"name": "Saas", "outfit": "sari, stick in hand"}]))
    assert prompt_checks("a comedy", [{"name": "Saas", "outfit": "sari", "image_path": "/s.png"}]) == []  # pictured
    assert prompt_checks("a comedy", [{"name": "Saas", "outfit": "red and teal striped sari"}]) == []


# ---- the prompt's details win over saved ones, field by field --------------------------------
def test_prompt_details_replace_saved_ones_field_by_field():
    female = {"id": 3, "name": "Female", "appearance": "young woman, braid", "outfit": "green lehenga",
              "voice": "Urdu accent", "image_path": "/f.png"}
    saas = {"id": 11, "name": "Saas", "appearance": "60 age, grey bun", "outfit": "sari", "voice": "elderly",
            "image_path": None}
    bible = script.normalize_bible({"cast": [
        {"name": "female", "outfit": "pink shalwar kameez", "voice": "young, Pakistani Punjabi accent", "extra": 1},
        {"name": "Saas", "age": "in her 50s", "outfit": "orange shalwar kameez"}, {"name": "Nobody"}]})
    assert [c["name"] for c in bible["cast"]] == ["female", "Saas"] and "extra" not in bible["cast"][0]
    sc = script.normalize_scene({"characters": [{"id": 3, "position": "frame right"}, {"id": 11}],
                                 "dialogue": [{"id": 3, "name": "Female", "line": "Ji"},
                                              {"id": 11, "name": "Saas", "line": "Haan"}]}, [female, saas])
    p = script.compile_prompt(sc, [female, saas], bible=bible)
    # pictured: the picture's face and hair, the prompt's dress (so "outfit" isn't in the "same …" list)
    assert "Female, frame right, the same face, hair and style as reference image 1, wearing pink shalwar kameez." in p
    assert "Female (voice: young, Pakistani Punjabi accent) says: 'Ji'" in p  # the prompt's voice
    # unpictured: the prompt's age (the saved "60 age" dropped) and outfit; the saved voice, which the prompt didn't give
    assert "Saas (age: in her 50s; grey bun; orange shalwar kameez)." in p and "Saas (voice: elderly) says" in p
    plain = script.compile_prompt(sc, [female, saas])  # no prompt details: everything as saved
    assert "Female, frame right, exactly as in reference image 1 (same face, hair, outfit and style)." in plain
    assert "Female (voice: Urdu accent) says" in plain and "Saas (60 age, grey bun; sari)." in plain


def test_review_edits_the_prompts_details_for_a_picked_character(app, authed, drive_connected, net):
    cid = add_character(authed).json()["id"]
    gid = authed.post("/generations", data={"scenes": ["Ali waves"], "aspect": "9:16", "review": "1",
                                            "cast": [str(cid)]}).json()["id"]
    r = authed.post(f"/generations/{gid}/script", json={
        "scenes": [{"action": "Ali waves", "characters": [{"id": cid}]}],
        "bible": {"cast": [{"name": "Ali", "outfit": "blue shalwar kameez"}]}})
    assert "Ali (man in his 30s, beard; blue shalwar kameez)." in r.json()["scenes"][0]["prompt"]
    assert gen(app, gid).bible["cast"] == [{"name": "Ali", "age": "", "look": "", "outfit": "blue shalwar kameez",
                                            "voice": ""}]


# ---- watch and remake one scene ---------------------------------------------------------------
def test_remake_one_scene_keeps_the_others_and_replaces_the_drive_copy(app, authed, drive_connected, net, tmp_path):
    from tests.test_app import submit_scenes
    for job in ("job1", "job2", "job3"):
        net.worker.clips[job] = clip_bytes(tmp_path, job)
    gid = submit_scenes(authed, ["a boat at dawn", "the boat at noon"]).json()["id"]
    finish(net, app, "job1")
    finish(net, app, "job2")
    g = gen(app, gid)
    assert g.status == GenStatus.READY and all(sc.clip_path for sc in g.scenes)
    first_drive, kept = g.drive_file_id, g.scenes[0].clip_path
    clip = authed.get(f"/generations/{gid}/scenes/2/clip")
    assert clip.status_code == 200 and clip.headers["content-type"] == "video/mp4"
    assert '"clips": [true, true]' in authed.get(f"/generations/{gid}/script").text  # app.js shows each clip

    assert authed.post(f"/generations/{gid}/scenes/2/redo").status_code == 200
    g = gen(app, gid)
    assert g.status == GenStatus.GENERATING and g.scenes[0].clip_path == kept and g.scenes[1].clip_path is None
    assert net.worker.submits[-1]["path"] == "/videos" and b"the boat at noon" in net.worker.submits[-1]["content"]
    assert len(net.worker.submits) == 3  # only scene 2 is made again
    finish(net, app, "job3")
    g = gen(app, gid)
    assert g.status == GenStatus.READY and g.drive_file_id != first_drive and g.old_drive_file_id is None
    assert net.google.files[first_drive]["trashed"]  # the old video went to the Drive trash once the new one was saved


def test_remake_is_refused_while_working_or_without_the_other_clips(app, authed, drive_connected, net):
    from tests.test_app import submit_scenes
    gid = submit_scenes(authed, ["a", "b"]).json()["id"]
    r = authed.post(f"/generations/{gid}/scenes/1/redo")
    assert r.status_code == 400 and "once the video is ready" in r.json()["detail"]
    assert authed.get(f"/generations/{gid}/scenes/1/clip").status_code == 404


# ---- one music track, even loudness, checked length -------------------------------------------
def _music(tmp_path):
    import subprocess
    path = tmp_path / "track.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=4", str(path)],
                   check=True)
    return path.read_bytes()


def test_join_lays_one_track_under_the_whole_video(tmp_path):
    from app.video_join import _probe, join_clips
    from tests.fakes import make_clip
    a, b = make_clip(tmp_path / "a.mp4", 3.0), make_clip(tmp_path / "b.mp4", 2.0)
    (tmp_path / "m.mp3").write_bytes(_music(tmp_path))
    join_clips([a, b], tmp_path / "out.mp4", music=tmp_path / "m.mp3")  # the 4 s track loops to fill 5 s
    info = _probe(tmp_path / "out.mp4")
    assert info["audio"] and abs(info["duration"] - 5.0) < 0.5


def test_music_upload_goes_under_the_video_and_clips_make_none(app, authed, drive_connected, net, tmp_path):
    net.worker.clips["job1"] = clip_bytes(tmp_path, "job1")
    r = authed.post("/generations", data={"prompt": "a paper boat", "aspect": "9:16"},
                    files=[("music", ("track.mp3", _music(tmp_path), "audio/mpeg"))])
    gid = r.json()["id"]
    g = gen(app, gid)
    assert g.music_path.endswith("music.mp3") and g.scenes[0].prompt.endswith(script.NO_MUSIC)
    finish(net, app, "job1")
    assert gen(app, gid).status == GenStatus.READY  # joined with the track (a single clip is re-encoded for it)
    bad = authed.post("/generations", data={"prompt": "a boat", "aspect": "9:16"},
                      files=[("music", ("notes.txt", b"hello there", "text/plain"))])
    assert bad.status_code == 400 and "isn't an MP3" in bad.json()["detail"]


# ---- "Lock characters" and "Check each clip" ----------------------------------------------------
def _two_scene_video(app, authed, net, **options):
    cid = add_character(authed, image=True).json()["id"]
    data = {"prompt": "Ali flies a kite", "duration": "20", "aspect": "9:16", "cast": [str(cid)], **options}
    gid = authed.post("/generations", data=data).json()["id"]
    net.worker.jobs["job1"].update(status="done", scenes=[{"characters": [{"name": "Ali"}], "action": "Ali runs."},
                                                          {"characters": [{"name": "Ali"}], "action": "Ali waves."}])
    tick(app)
    return gid


def test_lock_characters_draws_a_first_frame_then_starts_the_clip_from_it(app, authed, drive_connected, net,
                                                                          tmp_path):
    from tests.fakes import PNG
    gid = _two_scene_video(app, authed, net, lock_frames="1")
    still = net.worker.submits[1]
    assert still["path"] == "/stills" and still["images"] == 1  # Ali's picture (scene 1 has no previous clip)
    assert b"The first moment: Ali runs." in still["content"]
    assert "Locking scene 1" in authed.get("/api/generations").json()["cards"][0]["html"]
    net.worker.clips["job2"] = PNG  # the still
    net.worker.jobs["job2"].update(status="done", image=True)
    tick(app)
    g = gen(app, gid)
    assert g.scenes[0].still_path.endswith("scene-1-still.png")
    clip = net.worker.submits[2]
    assert clip["path"] == "/videos" and clip["images"] == 2  # Ali's picture, then the scene's own first frame
    assert script.STARTS_FROM.format(n=2).encode() in clip["content"]


def test_a_failed_still_lets_the_clip_go_ahead(app, authed, drive_connected, net):
    gid = _two_scene_video(app, authed, net, lock_frames="1")
    net.worker.jobs["job2"].update(status="failed", error="Gemini's image limit has been reached.")
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.GENERATING and g.scenes[0].still_path == ""
    assert net.worker.submits[2]["path"] == "/videos" and net.worker.submits[2]["images"] == 1


def test_check_each_clip_watches_the_clip_and_remakes_it_once(app, authed, drive_connected, net, tmp_path):
    import json as _json
    import re as _re
    for job in ("job2", "job4", "job6"):
        net.worker.clips[job] = clip_bytes(tmp_path, job)
    gid = _two_scene_video(app, authed, net, check_clips="1")
    finish(net, app, "job2")  # scene 1's clip arrives: Gemini watches it, with Ali's picture
    check = net.worker.submits[2]
    assert check["path"] == "/checks" and (check["images"], check["videos"], check["stills"]) == (1, 1, 4)
    body = _json.loads(_re.search(rb'name="body"\r\n\r\n(.*?)\r\n--', check["content"], _re.S).group(1))
    assert body["names"] == ["Ali"] and body["action"] == "Ali runs." and body["previous"] is False
    assert "Checking scene 1" in authed.get("/api/generations").json()["cards"][0]["html"]
    net.worker.jobs["job3"].update(status="done", check={"ok": False, "problems": "Ali never runs",
                                                         "failed": ["story_happened"]})
    tick(app)
    g = gen(app, gid)
    assert g.scenes[0].clip_path is None and g.scenes[0].redos == 1
    assert g.scenes[0].check_note == "Remade once: story didn't happen; Ali never runs"
    remake = net.worker.submits[3]
    assert remake["path"] == "/videos" and b"Fix from the last try: story didn't happen; Ali never runs." in \
        remake["content"]
    finish(net, app, "job4")  # the remake isn't checked again: scene 2 goes ahead
    assert net.worker.submits[4]["path"] == "/videos" and net.worker.submits[4]["images"] == 2  # + scene 1's last frame
    net.worker.jobs["job5"]["status"] = "done"
    net.worker.clips["job5"] = net.worker.clips["job6"]
    tick(app)
    check2 = net.worker.submits[5]
    assert check2["path"] == "/checks" and (check2["images"], check2["videos"]) == (1, 2)  # + scene 1's clip
    net.worker.jobs["job6"].update(status="done", check={"ok": True, "problems": ""})
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.READY and g.scenes[1].check_note == "Matches the pictures"


def test_busy_hands_are_warned_about_and_everyone_has_two_hands():
    sc = {"action": "Saas holds a huge roti in both hands and gestures dramatically toward Female."}
    assert script.busy_hands(sc) and "third hand" in script.scene_warnings(sc, "")[0]
    assert not script.busy_hands({"action": "Saas holds the roti in her left hand and points with her right."})
    assert not script.busy_hands({"action": "She carries the tray in both hands to the table."})
    assert "exactly two arms and two hands" in script.compile_prompt({"action": "x", "setting": "y"}, [])


def test_a_long_scene_still_continues_from_the_last_frame(app, authed, drive_connected, net, tmp_path):
    # the scene's prompt fills the 2000 characters: its action is shortened, the continuation is never dropped
    cid = add_character(authed, image=True).json()["id"]
    net.worker.clips["job2"] = clip_bytes(tmp_path, "job2")
    gid = authed.post("/generations", data={"prompt": "Ali flies a kite", "duration": "20", "aspect": "9:16",
                                            "cast": [str(cid)]}).json()["id"]
    long = "Ali runs across the rooftop with the kite, laughing. " * 40
    net.worker.jobs["job1"].update(status="done", scenes=[{"characters": [{"name": "Ali"}], "action": long},
                                                          {"characters": [{"name": "Ali"}], "action": long}])
    tick(app)
    assert len(gen(app, gid).scenes[1].prompt) >= 1990  # built to fill the limit
    finish(net, app, "job2")
    second = net.worker.submits[2]
    sent = second["content"].split(b'name="prompt"\r\n\r\n')[1].split(b"\r\n--")[0].decode()
    assert second["images"] == 2 and sent.endswith(script.CONTINUE_FROM.format(n=2)) and len(sent) <= 2000


def test_too_many_story_moments_for_the_length_are_flagged():
    from app.checks import prompt_checks
    five = "0–4 seconds: a. 4–8 seconds: b. 8–13 seconds: c. 13–16 seconds: d. 16–18 seconds: e."
    note = [n for n in prompt_checks(five, [], seconds=20) if "moments" in n]
    assert note and "5 moments" in note[0] and "Choose 30 seconds" in note[0]
    assert not [n for n in prompt_checks(five, [], seconds=30) if "moments" in n]
    assert not [n for n in prompt_checks("a boat drifts", [], seconds=10) if "moments" in n]


def test_lock_characters_draws_only_the_first_scene_then_starts_from_the_real_last_frame(
        app, authed, drive_connected, net, tmp_path):
    from tests.fakes import PNG
    gid = _two_scene_video(app, authed, net, lock_frames="1")
    net.worker.clips["job2"] = PNG
    net.worker.jobs["job2"].update(status="done", image=True)  # scene 1's still
    net.worker.clips["job3"] = clip_bytes(tmp_path, "job3")
    tick(app)
    finish(net, app, "job3")  # scene 1's clip
    second = net.worker.submits[3]
    assert second["path"] == "/videos" and second["images"] == 2  # no second still: Ali + scene 1's last frame
    assert script.STARTS_FROM.format(n=2).encode() in second["content"]
    assert gen(app, gid).scenes[1].still_path is None


def test_a_failed_remake_keeps_the_first_version(app, authed, drive_connected, net, tmp_path):
    for job in ("job2", "job5"):
        net.worker.clips[job] = clip_bytes(tmp_path, job)
    gid = _two_scene_video(app, authed, net, check_clips="1")
    finish(net, app, "job2")  # scene 1 → check (job3) says it's off → remake (job4)
    net.worker.jobs["job3"].update(status="done", check={"ok": False, "problems": "no kiss",
                                                         "failed": ["story_happened"]})
    tick(app)
    assert gen(app, gid).scenes[0].clip_path is None
    net.worker.jobs["job4"].update(status="failed", error="Gemini declined this clip for safety reasons.")
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.GENERATING and g.scenes[0].clip_path.endswith("scene-1.mp4")
    assert "so the first version is kept" in g.scenes[0].check_note
    tick(app)
    assert net.worker.submits[-1]["path"] == "/videos"  # scene 2 goes ahead


def test_minors_refusal_is_a_safety_stop():
    from worker.gemini_browser import StopText, load_ui
    text = "I can't make videos that seem to show minors in dangerous or compromising situations. Can I help you?"
    assert StopText(load_ui()).classify(text) == "safety"


def test_a_young_character_in_romance_or_hitting_is_flagged():
    from app.checks import prompt_checks
    story = "Pappu, a chubby young boy, blows a kiss at his phone. His mother chases him with a slipper."
    assert any("may take them for a child" in n for n in prompt_checks(story, []))
    # "young man" says adult: no error (the planner gives him an adult look), but a warning to make it certain
    from app.checks import prompt_notes
    man = story.replace("young boy", "young Punjabi man")
    assert [n["level"] for n in prompt_notes(man, [])] == ["warning"]
    assert "thick mustache" in prompt_checks(man, [])[0]
    adult = story.replace("chubby young boy", "chubby man in his 30s with a thick mustache")
    assert not any("for a child" in n for n in prompt_checks(adult, []))
    assert not any("for a child" in n for n in prompt_checks("A young girl flies a kite.", []))


# ---- fixing a failed video ----------------------------------------------------------------------
def test_a_failed_video_can_be_fixed_in_its_script_and_retried(app, authed, drive_connected, net, tmp_path):
    net.worker.clips["job2"] = clip_bytes(tmp_path, "job2")
    gid = _two_scene_video(app, authed, net)
    finish(net, app, "job2")  # scene 1 made
    net.worker.jobs["job3"].update(status="failed", error="Gemini declined this clip for safety reasons.")
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.FAILED
    card = authed.get("/api/generations").json()["cards"][0]["html"]
    assert "Fix script" in card
    page = authed.get(f"/generations/{gid}/script").text
    assert "Save and retry" in page and '"failed": true' in page
    scenes = [sc.script() for sc in g.scenes]
    scenes[1]["action"] = "Ali smiles and waves goodbye."
    r = authed.post(f"/generations/{gid}/script", json={"scenes": scenes[:1]})
    assert r.status_code == 400 and "don't add or remove" in r.json()["detail"]
    r = authed.post(f"/generations/{gid}/script", json={"scenes": scenes, "approve": True,
                                                         "bible": {"characters": [{"name": "Zed", "look": "x"}]}})
    assert r.status_code == 200
    g = gen(app, gid)
    assert g.status == GenStatus.GENERATING and g.scenes[0].clip_path  # scene 1 kept
    assert b"Ali smiles and waves goodbye." in net.worker.submits[-1]["content"]  # scene 2 made from the fix


def test_remake_works_while_later_scenes_are_not_made_yet(app, authed, drive_connected, net, tmp_path):
    net.worker.clips["job2"] = clip_bytes(tmp_path, "job2")
    gid = _two_scene_video(app, authed, net)
    finish(net, app, "job2")
    net.worker.jobs["job3"].update(status="failed", error="Gemini declined this clip for safety reasons.")
    tick(app)
    assert authed.post(f"/generations/{gid}/scenes/1/redo").status_code == 200  # scene 2 was never made
    g = gen(app, gid)
    assert g.status == GenStatus.GENERATING and g.scenes[0].clip_path is None
    assert b"Ali runs." in net.worker.submits[-1]["content"]  # scene 1 first, then scene 2 follows


def test_prompt_rules_page(authed):
    page = authed.get("/rules")
    assert page.status_code == 200 and "What only you can do" in page.text and "The app does this for you" in page.text
    assert 'data-copy="#prompt-maker"' in page.text and 'id="prompt-format"' in page.text
    assert 'href="/rules"' in authed.get("/").text  # in the menu and above the prompt



def test_spoken_words_fit_the_clip_length():
    assert script.words_per_clip(10) == 25 and script.words_per_clip(8) == 20
    scene = {"dialogue": [{"name": "Ali", "line": " ".join(["word"] * 22)}]}
    assert not any("spoken words" in w for w in script.scene_warnings(scene, "p"))  # 22 fit in 10 s
    assert any("about 20 fit in 8 seconds" in w for w in script.scene_warnings(scene, "p", seconds=8))


def test_still_prompt_describes_people_as_the_clip_does():
    """The locked first frame shows everyone as the clip will: what the prompt says (outfit, look) wins over the saved
    character and their picture, and a person with no picture gets the bible's look, so nothing changes mid-clip."""
    cast = [{"id": 1, "name": "Ali", "appearance": "slim man in his 30s", "outfit": "orange qameez",
             "image_path": "var/characters/1.jpg"}]
    bible = {"cast": [{"name": "Ali", "outfit": "white shalwar kameez and a colorful turban"}],
             "characters": [{"name": "Seller", "look": "thick mustache, brown kameez and a Sindhi topi"}]}
    scene = {"setting": "village street", "characters": [{"id": 1, "name": "Ali", "position": "frame left"},
                                                         {"id": None, "name": "Seller", "position": "frame right"}],
             "action": "Ali smiles. He runs."}
    text = script.still_prompt(scene, cast, "cartoon", bible)
    assert "Ali, frame left (picture 1), wearing white shalwar kameez and a colorful turban (not the clothes in the " \
           "picture)" in text
    assert "Seller (thick mustache, brown kameez and a Sindhi topi), frame right" in text
    assert "orange qameez" not in text
    plain = script.still_prompt(scene, cast, "cartoon", {})
    assert "Ali, frame left (picture 1)." in plain  # nothing in the prompt: the picture carries their look



# A prompt written out in full (people with looks and voices, timed lines, closing sound note), as in video #76
WRITTEN = ("A 16-second Pakistani Punjabi village comedy, Pixar-style 3D cartoon animation.\n\n"
           "Setting: A rural Punjabi village courtyard in daylight, a charpai and a clay water pot.\n\n"
           "Shinda: A man in his 30s, thick black mustache, wearing a mustard shalwar kameez and red turban. "
           "Voice: Overconfident, lively.\n\n"
           "Billa: A man in his 30s, short black hair, stubble, wearing a blue shalwar kameez and brown waistcoat. "
           "Voice: Teasing, deep.\n\n"
           "0–8 seconds: Shinda proudly shows his phone to Billa. Shinda says: \"I'm a mobile expert now!\"\n\n"
           "8–16 seconds: The phone rings and Shinda panics. Billa laughs and asks: \"Why don't you answer?\" "
           "Shinda replies: \"Who knows who's inside it!\"\n\n"
           "Sound: Phone ringtone, birds chirping, hearty laughter. No background music, no on-screen text.")


def test_backup_script_keeps_the_prompts_people_lines_and_sound():
    """When Gemini's script fails, the prompt's own one keeps who everyone is (look and voice), every spoken line as
    dialogue, and the closing sound note in every scene, so each clip prompt describes them and asks for lip-sync."""
    from app.timeline import bible_from_prompt, script_from_timeline
    bible = script.normalize_bible(bible_from_prompt(WRITTEN, []))
    assert [(c["name"], c["voice"]) for c in bible["characters"]] == [("Shinda", "Overconfident, lively"),
                                                                     ("Billa", "Teasing, deep")]
    assert "mustard shalwar kameez and red turban" in bible["characters"][0]["look"]
    scenes = script_from_timeline(WRITTEN, 2, [], clip_seconds=8)
    assert [[(d["name"], d["line"]) for d in s["dialogue"]] for s in scenes] == [
        [("Shinda", "I'm a mobile expert now!")],
        [("Billa", "Why don't you answer?"), ("Shinda", "Who knows who's inside it!")]]
    assert all(s["sound"].startswith("Phone ringtone") for s in scenes)
    assert all('"' not in s["action"] for s in scenes) and "Sound:" not in scenes[1]["action"]
    sc = script.ensure_cast(script.normalize_scene(scenes[1], []), [], bible)
    text = script.compile_prompt(sc, [], "cartoon", aspect="9:16", fit=True, bible=bible, music=False)
    assert "Billa (A man in his 30s, short black hair" in text and "brown waistcoat" in text
    assert "Billa (voice: Teasing, deep) says: 'Why don't you answer?'" in text
    assert "in sync with their lips" in text and script.NO_TEXT in text


def test_a_picked_character_the_prompt_describes_gets_its_words():
    from app.timeline import bible_from_prompt
    b = bible_from_prompt(WRITTEN, [{"name": "Billa"}])
    assert [c["name"] for c in b["characters"]] == ["Shinda"] and b["cast"][0]["name"] == "Billa"
    assert "brown waistcoat" in b["cast"][0]["look"] and b["cast"][0]["voice"] == "Teasing, deep"


def test_spoken_lines_are_not_on_screen_text():
    spoken = {"action": 'Billa laughs and asks: "Why don\'t you answer?"'}
    assert script.NO_TEXT in script.quality_rules(spoken)
    sign = {"action": 'A sign above the shop reads "Biryani House".'}
    assert script.NO_TEXT not in script.quality_rules(sign)


def test_note_when_picked_characters_are_not_in_the_prompt():
    from app.checks import prompt_notes
    notes = [n["text"] for n in prompt_notes(WRITTEN, [{"name": "Engineer"}], seconds=16, clip=8)]
    assert any("You picked Engineer, but the prompt never names this character: its people are Shinda, Billa"
               in n for n in notes)
    assert not any("You picked" in n for n in prompt_notes(WRITTEN, [{"name": "Billa"}], seconds=16, clip=8))
    assert not any("You picked" in n["text"] for n in prompt_notes("A man flies a kite.", [{"name": "Ali"}]))



# ---- rules from reviewing a Flow video (#77): roles, prompt-described looks, lettering, one style, whose line ------
CHACHA = {"id": 7, "name": "Chacha", "appearance": "50 man, white hairs, fatty body",
          "outfit": "qameez, shalwar, parna and pagh", "image_path": "var/characters/7.jpg"}
SELLER_BIBLE = {"cast": [{"name": "Chacha", "age": "in his 50s", "look": "thick black mustache, round face",
                          "outfit": "wearing a white shalwar kameez and green turban"}],
                "characters": [{"name": "Biryani Seller", "look": "a man in his 30s, trimmed beard, blue shalwar kameez"}]}
STALL = {"setting": "A Punjabi village street with a biryani stall",
         "characters": [{"id": 7, "name": "Chacha", "position": "frame left"},
                        {"id": None, "name": "Biryani Seller", "position": "frame right"}],
         "action": "Chacha grabs a plate. The seller holds out his hand.",
         "dialogue": [{"id": 7, "name": "Chacha", "line": "Free biryani!"},
                      {"id": None, "name": "Biryani Seller", "line": "Pay first!"}]}


def test_prompt_described_look_takes_only_the_face_from_the_picture():
    text = script.compile_prompt(STALL, [CHACHA], "Pixar-style, cartoon", bible=SELLER_BIBLE)
    assert "Chacha, frame left, the same face and style as reference image 1, age: in his 50s, thick black " \
           "mustache, round face, wearing a white shalwar kameez and green turban." in text
    assert "wearing wearing" not in text


def test_whose_line_is_whose_and_in_order():
    text = script.compile_prompt(STALL, [CHACHA], "Pixar-style, cartoon", bible=SELLER_BIBLE)
    assert script.SPEAKERS in text and script.SPEAKERS_IN_ORDER in text
    one = dict(STALL, dialogue=STALL["dialogue"][:1])
    assert script.SPEAKERS_IN_ORDER not in script.compile_prompt(one, [CHACHA], bible=SELLER_BIBLE)


def test_no_lettering_and_one_style_for_everyone():
    text = script.compile_prompt(STALL, [CHACHA], "Pixar-style, cartoon", bible=SELLER_BIBLE)
    assert "no written words on signs, flags or objects" in text and "lettering on signs, flags or banners" in text
    assert "Everyone in the same Pixar-style cartoon style, none more realistic." in text
    alone = script.compile_prompt({"setting": "a quiet bedroom", "action": "Ali sleeps."}, [], "cartoon")
    assert "lettering on signs" not in alone and "same cartoon style" not in alone  # no signage place, one person


def test_planner_asks_for_places_roles_and_whose_line():
    import worker.gemini_browser as gb
    from worker.generator import PlanRequest
    text = gb.plan_instruction(PlanRequest("j", "x", "9:16", 1, clip_seconds=8))
    assert "say where each person is in relation to the main place or object and what their role there is" in text
    assert "never give one person's line to another" in text


def test_next_scene_starts_from_the_very_last_frame(tmp_path):
    """Scene 2 continues from scene 1's final frame (8.0 s of 8), not one a quarter second earlier."""
    import subprocess
    from app.video_join import last_frame
    clip = tmp_path / "scene-1.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x568:r=24:d=7.88",
                    "-f", "lavfi", "-i", "color=c=blue:s=320x568:r=24:d=0.12",
                    "-filter_complex", "[0][1]concat=n=2:v=1:a=0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(clip)], check=True)  # red, then only its last 3 frames blue
    frame = last_frame(clip, tmp_path / "scene-1-last.jpg")
    rgb = subprocess.run(["ffmpeg", "-v", "error", "-i", str(frame), "-vf", "scale=1:1", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    r, g, b = rgb[:3]
    assert b > 150 and r < 100, (r, g, b)  # the blue final frame
