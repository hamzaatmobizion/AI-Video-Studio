"""The Flow screen: the same Studio, with every clip made in Google Flow (Veo 3.1 - Lite, Flow credits, 8 s scenes)."""
import json
from pathlib import Path

from app.enums import GenStatus
from app.routes.api import duration_from_prompt
from tests.test_app import clip_bytes, finish, gen, tick
from tests.test_script import add_character

OUT_OF_CREDITS = {"kind": "limit_reached", "provider": "flow",
                  "message": "Google Flow is out of credits. Flow videos are paused."}


def flow_idea(client, prompt="a day in the life of a lighthouse keeper", duration=24, **extra):
    data = {"prompt": prompt, "duration": str(duration), "aspect": "9:16", "provider": "flow", **extra}
    return client.post("/generations", data=data)


def test_flow_page_has_eight_second_scenes(authed, drive_connected):
    html = authed.get("/flow").text
    assert 'name="provider" value="flow"' in html and "Veo 3.1 - Lite" in html
    assert '<option value="8"' in html and '<option value="96"' in html and '<option value="10"' not in html
    assert 'href="/flow" class="active"' in html
    studio = authed.get("/").text
    assert 'name="provider" value="gemini"' in studio and '<option value="10"' in studio


def test_flow_video_plans_eight_second_scenes_and_makes_clips_in_flow(app, authed, drive_connected, net, tmp_path):
    for job in ("job2", "job3", "job4"):
        net.worker.clips[job] = clip_bytes(tmp_path, job)
    gid = flow_idea(authed).json()["id"]
    g = gen(app, gid)
    assert g.provider == "flow" and g.status == GenStatus.PLANNING and len(g.scenes) == 3  # 24 s = 3 × 8 s
    plan = net.worker.submits[0]
    assert plan["path"] == "/plans" and plan["json"]["clip_seconds"] == 8
    net.worker.jobs["job1"].update(status="done", scenes=["dawn", "storm", "night"])
    tick(app)
    clip = net.worker.submits[1]
    assert clip["path"] == "/videos" and clip["json"]["provider"] == "flow"
    for job in ("job2", "job3", "job4"):
        finish(net, app, job)
    g = gen(app, gid)
    assert g.status == GenStatus.READY, g.error
    later = net.worker.submits[2:]  # with the previous scene's last frame attached, so multipart
    assert later and all(b'name="provider"\r\n\r\nflow' in s["content"] for s in later)


def test_gemini_video_sends_no_provider(app, authed, drive_connected, net):
    authed.post("/generations", data={"prompt": "a boat", "duration": "10", "aspect": "9:16"})
    assert "provider" not in net.worker.submits[0]["json"]


def test_flow_duration_steps(authed, drive_connected):
    assert flow_idea(authed, duration=10).status_code == 400
    r = flow_idea(authed, duration=104)
    assert r.status_code == 400 and "8 seconds to 1:36" in r.json()["detail"]
    assert flow_idea(authed, duration=8).status_code == 200
    assert duration_from_prompt("a 30-second video", 8) == 32 and duration_from_prompt("a 30-second video") == 30


def test_bad_provider_refused(authed, drive_connected):
    r = flow_idea(authed, provider="sora")
    assert r.status_code == 400 and "Gemini or Google Flow" in r.json()["detail"]


def test_each_screen_shows_its_own_cards(app, authed, drive_connected, net):
    flow_id = flow_idea(authed, duration=8).json()["id"]
    gemini_id = authed.post("/generations", data={"prompt": "a boat", "duration": "10", "aspect": "9:16"}).json()["id"]
    flow_cards = [c["id"] for c in authed.get("/api/generations?provider=flow").json()["cards"]]
    studio_cards = [c["id"] for c in authed.get("/api/generations?provider=gemini").json()["cards"]]
    assert flow_cards == [flow_id] and studio_cards == [gemini_id]
    assert f'data-id="{flow_id}"' in authed.get("/flow").text and f'data-id="{flow_id}"' not in authed.get("/").text
    assert 'class="badge flow"' in authed.get("/api/generations").json()["cards"][1]["html"]
    assert f'href="/flow?prompt_from={flow_id}"' in authed.get("/history").text


def test_flow_lock_characters_draws_the_first_frame_in_flow(app, authed, drive_connected, net, tmp_path):
    """As with Gemini, "Lock characters" draws the first scene's opening frame first, but in Flow (its Image mode and
    credits), so a Flow video uses no Gemini image."""
    import json as _json
    import re as _re
    from tests.fakes import PNG
    cid = add_character(authed, image=True).json()["id"]
    data = {"prompt": "Ali flies a kite", "duration": "16", "aspect": "9:16", "cast": [str(cid)],
            "provider": "flow", "lock_frames": "1"}
    gid = authed.post("/generations", data=data).json()["id"]
    net.worker.jobs["job1"].update(status="done", scenes=[{"characters": [{"name": "Ali"}], "action": "Ali runs."},
                                                          {"characters": [{"name": "Ali"}], "action": "Ali waves."}])
    tick(app)
    still = net.worker.submits[1]
    body = _json.loads(_re.search(rb'name="body"\r\n\r\n(.*?)\r\n--', still["content"], _re.S).group(1))
    assert still["path"] == "/stills" and body["provider"] == "flow" and still["images"] == 1  # Ali's picture
    assert "Locking scene 1" in authed.get("/api/generations").json()["cards"][0]["html"]
    net.worker.clips["job2"] = PNG
    net.worker.jobs["job2"].update(status="done", image=True)
    net.worker.clips["job3"] = clip_bytes(tmp_path, "job3")
    tick(app)
    first = net.worker.submits[2]
    # As with Gemini: Ali's picture as reference image 1, then the drawn frame as reference image 2
    assert first["path"] == "/videos" and first["images"] == 2
    assert b'name="provider"\r\n\r\nflow' in first["content"] and b'name="frame"\r\n\r\n1' in first["content"]
    assert b"Reference image 2 is this shot's first frame" in first["content"]
    assert b"as in reference image 1" in first["content"]
    assert gen(app, gid).scenes[0].still_path.endswith("scene-1-still.png")
    finish(net, app, "job3")
    second = net.worker.submits[3]
    # no second still: Ali's picture again, then scene 1's last frame
    assert second["path"] == "/videos" and second["images"] == 2
    assert b'name="frame"\r\n\r\n1' in second["content"] and b"as in reference image 1" in second["content"]
    assert sum(s["path"] == "/stills" for s in net.worker.submits) == 1


def test_flow_still_pauses_with_flow(app, authed, drive_connected, net):
    """A Flow still that runs out of Flow credits pauses the video under Flow's pause, not Gemini's."""
    net.worker.pauses = {"gemini": None, "chat": None, "flow": None}
    cid = add_character(authed, image=True).json()["id"]
    data = {"prompt": "Ali flies a kite", "duration": "8", "aspect": "9:16", "cast": [str(cid)],
            "provider": "flow", "lock_frames": "1"}
    gid = authed.post("/generations", data=data).json()["id"]
    net.worker.jobs["job1"].update(status="done", scenes=[{"characters": [{"name": "Ali"}], "action": "Ali runs."}])
    tick(app)
    assert net.worker.submits[1]["path"] == "/stills"
    net.worker.pauses["flow"] = OUT_OF_CREDITS
    net.worker.jobs["job2"].update(status="failed", error=OUT_OF_CREDITS["message"])
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.PAUSED and "Flow credits" in g.error


def test_out_of_flow_credits_pauses_the_flow_video_and_resume_lifts_flow(app, authed, drive_connected, net, tmp_path):
    net.worker.pauses = {"gemini": None, "chat": None, "flow": None}
    gid = flow_idea(authed, duration=8).json()["id"]
    assert net.worker.submits[0]["path"] == "/plans"  # an 8-second Flow prompt is scripted too
    net.worker.jobs["job1"].update(status="done", scenes=["a lighthouse keeper at dawn"])
    tick(app)
    net.worker.pauses["flow"] = OUT_OF_CREDITS
    net.worker.jobs["job2"].update(status="failed", error=OUT_OF_CREDITS["message"])
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.PAUSED and "out of credits" in g.error and "Flow credits" in g.error
    net.worker.clips["job3"] = clip_bytes(tmp_path, "job3")
    assert authed.post(f"/generations/{gid}/resume").status_code == 200
    assert net.worker.resumed_sites == ["flow"]
    finish(net, app, "job3")
    assert gen(app, gid).status == GenStatus.READY


def test_gemini_limit_doesnt_pause_a_flow_video(app, authed, drive_connected, net):
    limit = {"kind": "limit_reached", "provider": "gemini", "message": "Gemini's video limit has been reached."}
    net.worker.pauses = {"gemini": limit, "chat": None, "flow": None}
    gid = flow_idea(authed, duration=8).json()["id"]
    net.worker.jobs["job1"].update(status="failed", error="Google Flow failed to generate this clip.")
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.FAILED and "failed to generate" in g.error  # its own error, not Gemini's limit


def test_settings_shows_flow_panel(authed, net):
    net.worker.pauses = {"gemini": None, "chat": None, "flow": OUT_OF_CREDITS}
    net.worker.flow = {"state": "limit_reached", "message": OUT_OF_CREDITS["message"]}
    html = authed.get("/settings").text
    assert "Google Flow (Veo 3.1 - Lite)" in html and "Out of credits" in html
    assert 'data-post="/settings/flow/resume"' in html
    assert authed.post("/settings/flow/resume").status_code == 200 and net.worker.resumed_sites == ["flow"]
    assert authed.get("/settings/flow/state").json()["busy"] is False
    assert authed.post("/settings/google/folders").status_code != 404  # Drive routes aren't shadowed


def test_prompt_check_counts_flow_clips(authed):
    prompt = "Scene 1: a. Scene 2: b. Scene 3: c. Scene 4: d. Scene 5: e."
    body = {"prompt": prompt, "seconds": 16, "provider": "flow"}
    notes = authed.post("/api/prompt-check", content=json.dumps(body),
                        headers={"content-type": "application/json"}).json()["notes"]
    assert any("2 clips of 8 seconds" in n["text"] for n in notes)


def test_flow_keeps_its_length_and_stops_only_for_a_crammed_clip(app, authed, drive_connected, net):
    """A tight clip (up to 20 words) is made as written, never split into more scenes; only more than 20 words in
    8 seconds waits for the user to shorten a line."""
    line = "ek do teen char panch chhe saat aath"  # 8 words
    tight = [{"name": "Ali", "line": f"{line} one"}, {"name": "Sara", "line": f"{line} two"},
             {"name": "Ali", "line": "phir milenge"}]  # 20 words
    gid = flow_idea(authed, prompt=f'Ali says: "{line} one" and Sara says: "{line} two"', duration=16).json()["id"]
    net.worker.jobs["job1"].update(status="done", scenes=[{"action": "Ali waves.", "dialogue": tight},
                                                          {"action": "They walk off."}])
    tick(app)
    g = gen(app, gid)
    assert len(g.scenes) == 2 and g.status != GenStatus.REVIEW
    crammed = [*tight, {"name": "Ali", "line": "phir milenge yaar"}]  # 23 words
    gid = flow_idea(authed, prompt=f'Ali says: "{line} one" and Sara says: "{line} two"', duration=8).json()["id"]
    net.worker.jobs[gen(app, gid).worker_job_id].update(status="done", scenes=[{"action": "Ali waves.",
                                                                                "dialogue": crammed}])
    tick(app)
    g = gen(app, gid)
    assert len(g.scenes) == 1 and g.status == GenStatus.REVIEW


def test_flow_script_that_drops_a_line_waits_for_review(app, authed, drive_connected, net):
    gid = flow_idea(authed, prompt='Ali says: "chalo ghar chalein" and then "kal milenge yaar"', duration=8).json()["id"]
    net.worker.jobs["job1"].update(status="done", scenes=[
        {"action": "Ali waves.", "dialogue": [{"name": "Ali", "line": "Chalo, ghar chalein!"}]}])
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.REVIEW  # punctuation doesn't count; the second line is missing
    page = authed.get(f"/generations/{gid}/script").text
    assert "Not said in any scene" in page and "“kal milenge yaar”" in page and "“chalo ghar chalein”" not in page


def test_flow_screen_ticks_only_making_the_clips_by_hand(authed, drive_connected):
    flow = authed.get("/flow").text
    assert 'name="by_hand" value="1" checked' in flow
    assert 'name="review" value="1" checked' not in flow and 'name="check_clips" value="1" checked' not in flow
    studio = authed.get("/").text  # the Studio keeps its own defaults
    assert 'name="review" value="1" checked' in studio and 'name="check_clips" value="1" checked' not in studio



def test_flow_clip_where_someone_holds_something_and_claps(authed):
    """Video 133: Ammi "holding her wooden stick in her right hand" and "clapping": Veo clapped with both hands and the
    stick stood up on its own. On Flow the planner is told not to write it, the script page warns, and the clip
    prompt's "Objects" rule says what is held stays in hand; Gemini's Studio prompts are unchanged."""
    from app import genres, script
    scene = {"characters": [{"name": "Ammi"}],
             "action": "Ammi stands holding her wooden stick in her right hand, smiling and clapping to the beat."}
    assert script.holds_and_claps(scene) and not script.holds_and_claps({"action": "Ammi claps to the beat."})
    assert script.holds_and_claps({"action": "Ammi claps her left hand against her resting stick.",  # its scene 2
                                   "props": [{"name": "stick", "state": "held in ammi's right hand"}]})
    held = "what is held stays in that hand or is put down; nothing floats or stands up on its own"
    assert held in script.compile_prompt(scene, [], room=script.FLOW_MAX_PROMPT, flow=True)
    assert held not in script.compile_prompt(scene, [], room=script.FLOW_MAX_PROMPT)
    assert any("also claps" in w for w in script.scene_warnings(scene, "", seconds=script.FLOW_SECONDS))
    assert not any("also claps" in w for w in script.scene_warnings(scene, "", seconds=10))
    assert genres.FLOW_RULES[0] in genres.plan_rules("music", 2, flow=True)
    assert genres.FLOW_RULES[0] not in genres.plan_rules("music", 2)


def test_flow_music_video_lines_are_sung(authed):
    from app import script
    scene = {"characters": [{"name": "Bahu"}], "action": "Bahu dances.",
             "dialogue": [{"name": "Bahu", "line": "ساڈے دل وچ چاہواں نیں"}]}
    assert "Bahu sings: 'ساڈے دل وچ چاہواں نیں'" in script.compile_prompt(scene, [], genre="music", flow=True)
    assert "Bahu says:" in script.compile_prompt(scene, [], genre="music")  # Gemini Studio: as before
    assert "Bahu says:" in script.compile_prompt(scene, [], genre="comedy", flow=True)


def _cut_clip(path, cut_at=7.7, length=8.0):
    """A clip of one shot (a slowly moving gradient) that cuts to another (solid blue) at `cut_at`."""
    import subprocess
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=320x568:r=24:d={cut_at}",
                    "-f", "lavfi", "-i", f"color=c=blue:s=320x568:r=24:d={length - cut_at:.2f}",
                    "-filter_complex", "[0][1]concat=n=2:v=1:a=0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(path)], check=True)
    return path


def test_late_cut_to_another_shot_is_found_and_trimmed(tmp_path):
    from app.video_join import clip_seconds, late_cut, trim_clip
    clip = _cut_clip(tmp_path / "cut.mp4")
    assert abs(late_cut(clip) - 7.7) < 0.1
    assert trim_clip(clip, late_cut(clip)) and abs(clip_seconds(clip) - 7.7) < 0.1 and late_cut(clip) is None
    early = _cut_clip(tmp_path / "early.mp4", cut_at=3.0)  # a cut mid-clip is left for the clip check
    assert late_cut(early) is None


def test_flow_clip_uploaded_by_hand_loses_a_late_cut(app, authed, drive_connected, net, tmp_path):
    """Video 134: its last scene cut to other people in its final quarter second."""
    from app.video_join import clip_seconds
    from tests.test_app import by_hand
    gid = by_hand(authed, ["they dance", "they bow"]).json()["id"]
    clip = _cut_clip(tmp_path / "scene.mp4").read_bytes()
    authed.post(f"/generations/{gid}/scenes/1/clip", files={"clip": ("x.mp4", clip, "video/mp4")})
    assert abs(clip_seconds(Path(gen(app, gid).scenes[0].clip_path)) - 7.7) < 0.1


def test_flow_clip_prompt_ends_in_a_few_short_rules():
    """A Flow clip prompt closes with one short line each for the shot, people, objects, motion, speech and look,
    not the Studio's paragraphs, so the action keeps its room."""
    from app import script
    scene = {"setting": "a courtyard", "characters": [{"name": "Ali"}, {"name": "Sara"}], "action": "Ali waves.",
             "dialogue": [{"name": "Ali", "line": "Salaam"}, {"name": "Sara", "line": "Salaam ji"}]}
    flow = script.compile_prompt(scene, [], style="Pixar-style 3D cartoon", flow=True, music=False,
                                 room=script.FLOW_MAX_PROMPT)
    lines = flow.split("\n")
    rules = lines[next(i for i, x in enumerate(lines) if x.startswith("Shot:")):]
    assert [r.split(":")[0] for r in rules] == ["Shot", "People", "Objects", "Motion", "Speech", "Look"]
    # still about a third of the old rules; the People line now says what Veo adds when someone turns (video 156)
    assert len("\n".join(rules)) <= 1000 and script.CONTINUITY not in flow and script.ACTING not in flow
    assert "nothing added (no flowers in the hair, nose pin, extra braid or jewellery)" in flow
    assert "no stray hands at the edges" in flow
    assert "one Pixar-style 3D cartoon look for everyone" in flow and "no background music" in flow
    studio = script.compile_prompt(scene, [], style="Pixar-style 3D cartoon", room=script.FLOW_MAX_PROMPT)
    assert script.CONTINUITY in studio and "Shot: one continuous shot" not in studio



def test_flow_pictured_character_stays_exactly_as_their_picture():
    """A picked character with a portrait is shown exactly as in it, clothes included, whatever the prompt says."""
    from app import script
    cast = [{"id": 5, "name": "Bilal", "outfit": "navy suit", "image_path": "bilal.png"}]
    bible = {"cast": [{"name": "Bilal", "outfit": "blue kurta and white shalwar", "voice": "sharp"}]}
    scene = {"characters": [{"id": 5, "name": "Bilal"}], "action": "Bilal folds his arms."}
    prompt = script.compile_prompt(scene, cast, bible=bible, flow=True, room=script.FLOW_MAX_PROMPT)
    assert "exactly as in reference image 1 (same face, hair, outfit and style)" in prompt
    assert "blue kurta" not in prompt


def test_flow_people_keep_their_place_from_scene_to_scene(app, authed, drive_connected, net):
    """Video 136: scene 3 put Shani on the left where scene 2 ended with Bilal, so Veo (going by where people stand
    in the last frame) gave each the other's actions and lines. On Flow, everyone keeps their place."""
    from app import genres, script
    swapped = [{"characters": [{"name": "Bilal", "position": "frame left"}, {"name": "Shani", "position": "frame right"}],
                "action": "Bilal holds the plate."},
               {"characters": [{"name": "Shani", "position": "frame left"}, {"name": "Bilal", "position": "frame right"},
                               {"name": "Ammi", "position": "background"}],
                "action": "Shani on frame left squeezes a lemon. Bilal, on frame right, slaps his forehead."}]
    kept = script.keep_places(swapped)[1]
    assert [(p["name"], p["position"]) for p in kept["characters"]] == [
        ("Shani", "frame right"), ("Bilal", "frame left"), ("Ammi", "background")]
    assert kept["action"] == "Shani on frame right squeezes a lemon. Bilal, on frame left, slaps his forehead."
    assert "never swap who stands where" in genres.plan_rules("comedy", 3, flow=True)
    gid = flow_idea(authed, duration=16).json()["id"]
    net.worker.jobs["job1"].update(status="done", scenes=swapped)
    tick(app)
    assert [p["position"] for p in gen(app, gid).scenes[1].characters] == ["frame right", "frame left", "background"]


def test_flow_planner_follows_the_prompt_rules_page():
    """A one-line idea on Flow is written by Gemini as the Prompt rules page asks: a calm shot, who does what with
    which hand, carrying on from the scene before, short natural Punjabi lines, effects-only sound. Studio's plan
    rules are unchanged, and every type's rules still fit what the worker takes."""
    from app import genres
    flow = genres.plan_rules("comedy", 3, flow=True)
    for rule in ("one calm move", "which hand holds what", "carries on from where the one before ended",
                 "everyday Pakistani Punjabi in Shahmukhi", "never more than 20", "effects only",
                 "don't turn it into a comedy"):
        assert rule in flow
    assert "one calm move" not in genres.plan_rules("comedy", 3)
    assert all(len(genres.plan_rules(g, 12, flow=True)) <= 3000 for g in genres.GENRES)


def test_flow_line_written_with_its_voice_in_brackets_stays_in_place():
    """Video 154: 'Saas (voice: …) says: "…"' wasn't taken for a line (the ":" in the brackets), so the clip went to
    Gemini's rewrite, whose long "Bahu stands … and replies." left Bahu's line at the end, and Saas said it."""
    from app import script, timeline
    prompt = ('0–8 seconds: Saas sits on the charpai on frame left.\n\nSaas (voice: elderly female, deep) says: '
              '"بہو! صفائی کر لے!"\n\nBahu stands on frame right, holding the broom in her right hand. She places her '
              'left hand on her hip, looks at Saas with a cheeky smile and replies:\n\nBahu (voice: young female, '
              'cheeky) says: "امی جی، تسی وی تے گھر وچ او!"')
    assert timeline.prompt_voices(prompt) == {"saas": "elderly female, deep", "bahu": "young female, cheeky"}
    action, lines = timeline._speech(timeline._joined(timeline._scene_texts(prompt, 1, 8)[0]), ["Saas", "Bahu"])
    assert [d["name"] for d in lines] == ["Saas", "Bahu"] and "(voice" not in action
    long = ("Bahu stands on frame right holding the broom in her right hand, places her left hand on her hip, looks "
            "at Saas with a cheeky smile, and replies.")
    placed, done = script.speak_inline(long, [{"name": "Bahu", "line": "او!"}], {"bahu": "frame right"})
    assert done == {0} and placed.startswith("Bahu (frame right) stands") and placed.endswith('replies: "او!"')


def test_scene_headings_with_a_dash_dont_leak_into_the_clips():
    """Video 156: "SCENE 2 — 8–16 SECONDS:" left "SCENE 2 —." at the end of clip 1's action."""
    from app import timeline
    prompt = ("SCENE 1 — 0–8 SECONDS: She starts dancing.\n\nSCENE 2 — 8–16 SECONDS: She turns once.\n\n"
              "SCENE 3 — 16–24 SECONDS: She holds the final pose.\n\nDANCE STYLE: Gidda.")
    assert [t for t, in timeline._scene_texts(prompt, 3, 8)] == [
        "She starts dancing.", "She turns once.", "She holds the final pose."]


MASK_PROMPT = """A 16-second vertical 9:16 Pakistani Punjabi family comedy, Pixar-style 3D cartoon animation.
Setting: a cosy village courtyard at night, a woven charpai, a small steel bowl of green face-mask paste.

Bahu — Voice: young woman, calm and cheeky, Pakistani Punjabi accent.
Ammi: an old woman in a sari.
Voice: older woman, loud and dramatic.

0–8 seconds: Camera: medium shot, eye level, static. Bahu sits on the left, cucumber slices on her eyes. Ammi walks \
in on the right and jumps back. Ammi screams: "ہائے ربا! بھوت!" Bahu, not moving, says calmly: "امی جی، میں آں!"

8–16 seconds: Bahu lifts the cucumber slices off her eyes. Bahu says: "فیس ماسک اے!" Ammi rubs paste on her cheeks.

Sound: crickets at night."""


def test_flow_keeps_what_the_prompt_already_says(app, authed, drive_connected, net):
    """Video 139: Gemini's script gave Bahu her saved "Urdu accent, middle-aged" voice instead of the prompt's, and
    added a stick to Ammi's action. On Flow, the prompt's own actions, camera, lines and voices are kept as written;
    Gemini's script only adds places, objects and the rest."""
    bahu = add_character(authed, name="Bahu", voice="female, Urdu accent, middle-aged").json()["id"]
    ammi = add_character(authed, name="Ammi", voice="elderly, deep").json()["id"]
    gemini = [{"characters": [{"name": "Bahu", "position": "frame left"}, {"name": "Ammi", "position": "frame right"}],
               "action": "Bahu sits still. Ammi walks in holding her stick in her left hand.",
               "shot": {"framing": "Medium", "lens": "35mm"},
               "dialogue": [{"name": "Ammi", "line": "ہائے ربا!"}, {"name": "Bahu", "line": "امی جی، میں آں!"}],
               "props": [{"name": "steel bowl", "state": "on the charpai"}]},
              {"characters": [{"name": "Bahu", "position": "frame left"}, {"name": "Ammi", "position": "frame right"}],
               "action": "Bahu lifts the slices off. Ammi drops her stick and rubs paste on her cheeks.",
               "dialogue": [{"name": "Bahu", "line": "فیس ماسک اے!"}]}]
    gid = flow_idea(authed, prompt=MASK_PROMPT, duration=16, cast=[str(bahu), str(ammi)]).json()["id"]
    net.worker.jobs["job1"].update(status="done", scenes=gemini, bible={"cast": [
        {"name": "Bahu", "voice": "female, Urdu accent, middle-aged"}, {"name": "Ammi", "voice": "elderly, deep"}]})
    tick(app)
    g = gen(app, gid)
    first, second = g.scenes
    assert first.action == ("Camera: medium shot, eye level, static. Bahu sits on the left, cucumber slices on her "
                            "eyes. Ammi walks in on the right and jumps back. Ammi screams. Bahu, not moving, says "
                            "calmly.")
    assert [(d["name"], d["line"]) for d in first.dialogue] == [("Ammi", "ہائے ربا! بھوت!"), ("Bahu", "امی جی، میں آں!")]
    assert not any(first.shot.values()) and second.shot.get("lens", "") == ""
    assert "stick" not in second.action and first.props == [{"name": "steel bowl", "state": "on the charpai"}]
    assert [p["position"] for p in first.characters] == ["frame left", "frame right"]
    assert {c["name"]: c["voice"] for c in g.bible["cast"]} == {
        "Bahu": "young woman, calm and cheeky, Pakistani Punjabi accent", "Ammi": "older woman, loud and dramatic"}
    # each line where it happens, by the person on their side; their voices in one line
    assert ('Ammi walks in on the right and jumps back. Ammi (frame right) screams: "ہائے ربا! بھوت!" '
            'Bahu (frame left), not moving, says calmly: "امی جی، میں آں!"') in first.prompt
    assert "Voices: Ammi (frame right): older woman, loud and dramatic; Bahu (frame left): young woman, calm and " \
           "cheeky, Pakistani Punjabi accent." in first.prompt
    assert "Urdu accent" not in first.prompt and "Medium shot," not in first.prompt
    assert "completely static camera throughout" in first.prompt  # "Camera: … static"
    assert "doing only the move above" in second.prompt  # no camera written: Gemini's, as it moves


def test_flow_line_after_she_is_said_by_whom_the_sentence_before_is_about():
    """Video 143: "Bahu finds the cat sleeping. She turns to Saas and says: "…"" went to Gemini's rewrite (no name
    before "says") and lost when the lines are said; "She" is Bahu, not the cat."""
    from app import script, timeline
    action, lines = timeline._speech('Bahu finds the cat sleeping. She turns to Saas and says: "بلی اتھے اے!" '
                                     'Saas freezes, looks at the paratha in her hand, then confidently says: "اچھا!"',
                                     ["Bahu", "Saas", "Cat"])
    assert [(d["name"], d["line"]) for d in lines] == [("Bahu", "بلی اتھے اے!"), ("Saas", "اچھا!")]
    scene = {"action": action, "dialogue": lines,
             "characters": [{"id": 1, "name": "Bahu", "position": "frame left"},
                            {"id": 2, "name": "Saas", "position": "frame right"}]}
    cast = [{"id": 1, "name": "Bahu", "image_path": "b.png"}, {"id": 2, "name": "Saas", "image_path": "s.png"}]
    prompt = script.compile_prompt(scene, cast, bible={"palette": "warm yellow, olive green"}, flow=True,
                                   room=script.FLOW_MAX_PROMPT)
    assert 'She (Bahu, frame left) turns to Saas and says: "بلی اتھے اے!"' in prompt
    assert 'Saas (frame right) freezes' in prompt and "Colour palette" not in prompt  # the portraits set the colours
    studio = script.compile_prompt(scene, cast, bible={"palette": "warm yellow"}, room=script.FLOW_MAX_PROMPT)
    assert "Colour palette" in studio and "Bahu (voice" not in studio and "Bahu says: 'بلی اتھے اے!'" in studio
