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


def test_roman_punjabi_lines_written_out_in_shahmukhi_dont_stop_for_review(app, authed, drive_connected, net):
    """Video 168 stopped for review unasked: its lines were in Roman Punjabi and Gemini's script says them in
    Shahmukhi, as Pakistani Punjabi is spoken; that's the same lines, not dropped ones."""
    from app.timeline import missing_lines
    prompt = 'He says: "Oye! Main Punjab da sab ton smart banda aan!" His friend: "Oye! Teri taan izzat vi bakri kha gayi!"'
    both = [{"dialogue": [{"line": "اوئے! میں پنجاب دا سب توں سمارٹ بندا آں!"}]},
            {"dialogue": [{"line": "اوئے! تیری تاں عزت وی بکری کھا گئی!"}]}]
    assert missing_lines(prompt, both) == [] and len(missing_lines(prompt, both[:1])) == 2  # one really dropped
    gid = flow_idea(authed, prompt=prompt, duration=16).json()["id"]
    net.worker.jobs["job1"].update(status="done", scenes=[
        {"action": "He boasts.", "dialogue": [dict(name="Ali", **both[0]["dialogue"][0])]},
        {"action": "They laugh.", "dialogue": [dict(name="Sara", **both[1]["dialogue"][0])]}])
    tick(app)
    assert gen(app, gid).status != GenStatus.REVIEW


def test_what_the_story_changes_is_not_avoided(app):
    """Video 169: the goat ate Raju's fake moustache, but "Avoid: clean-shaven" (to keep him adult) and "same face
    … throughout" kept it on, so the punchline had nothing to laugh at. And "3D cartoon" came out as "Cartoon"."""
    from app import genres, script
    bible = {"characters": [{"name": "Raju", "look": "a man in his late 30s with a fake moustache",
                             "never": "clean-shaven, child-sized, beard"}]}
    eaten = {"characters": [{"name": "Raju"}], "action": "The goat eats Raju's fake moustache right off his face."}
    before = {"characters": [{"name": "Raju"}], "action": "Raju stands proudly with his fake moustache."}
    after = script.compile_prompt(eaten, [], bible=bible, flow=True, room=script.FLOW_MAX_PROMPT)
    assert "clean-shaven" not in after and "Avoid: child-sized, beard." in after
    assert "except what the action changes" in after and "all fully in frame, none cut off at the edges" in after
    assert "clean-shaven" in script.compile_prompt(before, [], bible=bible, flow=True, room=script.FLOW_MAX_PROMPT)
    knocked = dict(eaten, action="A gust knocks Raju's turban off.")
    bible2 = {"characters": [{"name": "Raju", "look": "a man", "never": "bare-headed, child-sized"}]}
    assert "bare-headed" not in script.compile_prompt(knocked, [], bible=bible2, flow=True, room=script.FLOW_MAX_PROMPT)
    rules = genres.plan_rules("comedy", 2, flow=True)
    assert "never list the result in 'never'" in rules and "is a character" in rules
    assert script.style_from_prompt("A 16-second hilarious Punjabi 3D cartoon comedy") == "3D cartoon"


def test_the_key_moment_is_possible_and_objects_say_only_where_they_start():
    """Video 170: a waist-high goat "chomped" a tall man's moustache in one crowded sentence, and never reached it;
    and the moustache was "stuck on his lip, then eaten completely" when the clip started."""
    from app import genres, script
    rules = genres.plan_rules("comedy", 2, flow=True)
    assert "Make every action physically possible" in rules and "bends down, crouches or sits" in rules
    assert "key moment gets its own short steps" in rules and len(rules) <= 3000
    scene = {"characters": [{"name": "Tariq"}], "action": "Tariq bends down. The goat bites the moustache.",
             "props": [{"name": "fake moustache", "state": "stuck on Tariq's upper lip, then eaten completely"}]}
    flow = script.compile_prompt(scene, [], flow=True, room=script.FLOW_MAX_PROMPT)
    assert "fake moustache: stuck on Tariq's upper lip." in flow and "then eaten" not in flow
    assert "then eaten completely" in script.compile_prompt(scene, [], room=script.FLOW_MAX_PROMPT)  # Studio as was


def test_a_quoted_title_is_not_a_missing_line():
    """Video 172 stopped for review unasked: 'titled: "پپو دا لیٹ ویاہ 😂"' was taken for a line nobody says."""
    from app.timeline import missing_lines
    prompt = 'A comedy short film titled:\n\n"پپو دا لیٹ ویاہ 😂"\n\nHis friend jokes: "اوئے پپو! ویاہ کدوں اے؟"'
    said = [{"dialogue": [{"line": "اوئے پپو! ویاہ کدوں اے؟"}]}]
    assert missing_lines(prompt, said) == [] and missing_lines(prompt, []) == ["اوئے پپو! ویاہ کدوں اے؟"]


def test_every_line_is_said_in_order_by_its_speaker_with_the_listener_silent():
    """Video 173: in nearly every clip one man's line came out of the other's mouth. "as he responds" (Pappu) took
    the Friend's line; the Friend's question, with no "says", went after the action; and nothing told the listener
    to keep quiet at the moment of each line."""
    from app import genres, script
    scene = {"characters": [{"name": "Pappu", "position": "frame left"}, {"name": "Friend", "position": "frame right"}],
             "action": "Friend leans forward, raising his eyebrows in curiosity. Pappu smiles proudly as he responds.",
             "dialogue": [{"name": "Friend", "line": "تے ہُن لبھ گئی؟"}, {"name": "Pappu", "line": "ہاں یار، لبھ گئی!"}]}
    text = script.compile_prompt(scene, [], flow=True, room=script.FLOW_MAX_PROMPT)
    assert ('Friend leans forward, raising his eyebrows in curiosity. Friend (frame right) says: "تے ہُن لبھ گئی؟" '
            '(Pappu silent, lips closed). Pappu (frame left) smiles proudly as he responds: "ہاں یار، لبھ گئی!" '
            '(Friend silent, lips closed).') in text
    assert "Each sounds clearly different (pitch, age, tone), never alike." in text
    joking = dict(scene, action="Friend gestures at Pappu while joking.", dialogue=scene["dialogue"][:1])
    assert 'Friend (frame right) gestures at Pappu while joking: "تے ہُن لبھ گئی؟"' in script.compile_prompt(
        joking, [], flow=True, room=script.FLOW_MAX_PROMPT)
    assert "One speaker per scene where the story allows" in genres.plan_rules("comedy", 8, flow=True)


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
    held = "held things stay in hand; nothing floats"
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
    assert 'Bahu sings: "ساڈے دل وچ چاہواں نیں"' in script.compile_prompt(scene, [], genre="music", flow=True)
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


def test_frames_page_gives_each_scenes_first_and_last_frame(app, authed, drive_connected, net, tmp_path):
    """Frames: each made scene's first and last frame, to download and use as a Start or End frame in Flow."""
    from tests.test_app import by_hand
    gid = by_hand(authed, ["they dance", "they bow"]).json()["id"]
    assert "No clips yet" in authed.get(f"/generations/{gid}/frames").text
    authed.post(f"/generations/{gid}/scenes/1/clip", files={"clip": ("x.mp4", clip_bytes(tmp_path, "s1", 2.0),
                                                                     "video/mp4")})
    page = authed.get(f"/generations/{gid}/frames").text
    assert "Scene 1 of 2" in page and "Scene 2 of 2" not in page and "Start frame for scene 2" in page
    for which in ("first", "last"):
        r = authed.get(f"/generations/{gid}/scenes/1/frame/{which}?download=1")
        assert r.status_code == 200 and r.content[:3] == b"\xff\xd8\xff"  # a JPG
        assert f"scene-1-{which}-frame.jpg" in r.headers["content-disposition"]
    assert authed.get(f"/generations/{gid}/scenes/2/frame/first").status_code == 404  # not made yet
    assert authed.get(f"/generations/{gid}/scenes/1/frame/middle").status_code == 404
    assert f'href="/generations/{gid}/frames"' in authed.get(f"/generations/{gid}/by-hand").text
    assert f'href="/generations/{gid}/frames"' in authed.get("/flow").text


def test_frames_tool_gives_any_videos_first_and_last_frame(app, authed, tmp_path):
    """Frames (in the menu): upload any video, get its first and last frame; nothing is kept."""
    import base64
    page = authed.get("/frames").text
    assert 'id="frames-form"' in page and 'href="/frames"' in page
    before = sorted(p.name for p in tmp_path.iterdir())
    r = authed.post("/api/frames", files={"video": ("dance.mp4", clip_bytes(tmp_path, "any", 2.0), "video/mp4")})
    assert r.status_code == 200
    for which in ("first", "last"):
        assert base64.b64decode(r.json()[which].split(",", 1)[1])[:3] == b"\xff\xd8\xff"  # a JPG
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(before + ["any.mp4"])  # nothing left behind here
    r = authed.post("/api/frames", files={"video": ("notes.txt", b"just some text", "text/plain")})
    assert r.status_code == 400 and "isn't an MP4, MOV or WebM" in r.json()["detail"]


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
    # short lines Veo follows: what it adds when someone turns (video 156), each object once and no writing
    # anywhere (video 158: two steering wheels, a garbled bus sign)
    assert len("\n".join(rules)) <= 950 and script.CONTINUITY not in flow and script.ACTING not in flow
    assert "exactly 2 main characters: Ali, Sara, with background people only if the place has them" in flow
    assert "add nothing (no flowers in the hair, nose pin, extra braid); all fully in frame" in flow
    assert "each only once (one door, one cup)" in flow and "no text, subtitles, logos or writing anywhere" in flow
    assert "one Pixar-style 3D cartoon look, never photorealistic" in flow and "no music." in flow
    assert "squash and stretch and follow-through" in flow


def test_flow_clip_ends_with_everyone_in_the_next_clip_in_frame(app, authed, drive_connected, net, tmp_path):
    """Video 161: the bunny hopped out of clip 1, whose last frame starts clip 2 with no pictures, so clip 2 drew a
    new bunny. Each clip but the last ends with whoever is also in the next one in view."""
    from app import genres
    from tests.test_app import by_hand
    gid = by_hand(authed, ["Phuppo and Ali walk; Bunny hops off.", "Bunny zigzags; they stop.", "Ali waves."]).json()["id"]
    with app.state.db.session() as s:
        from app.models import Generation
        g = s.get(Generation, gid)
        cast = [[{"name": "Phuppo"}, {"name": "Ali"}, {"name": "Bunny"}], [{"name": "Bunny"}, {"name": "Ali"}],
                [{"name": "Ali"}]]
        for scene, people in zip(g.scenes, cast):
            scene.characters = [{"id": None, "position": "", **p} for p in people]
    first = authed.get(f"/generations/{gid}/by-hand").text
    assert "End the clip with Ali and Bunny all clearly in frame: the next clip starts from its last frame." in first
    # Phuppo isn't in clip 2: she's gone by the end of clip 1, so clip 2 doesn't start by removing her
    assert "Phuppo leaves the frame naturally before the end and is not in the last frame." in first
    authed.post(f"/generations/{gid}/scenes/1/clip", files={"clip": ("x.mp4", clip_bytes(tmp_path, "s1"), "video/mp4")})
    second = authed.get(f"/generations/{gid}/by-hand").text
    assert "End the clip with Ali all clearly in frame" in second
    assert "Bunny leaves the frame naturally before the end" in second
    authed.post(f"/generations/{gid}/scenes/2/clip", files={"clip": ("x.mp4", clip_bytes(tmp_path, "s2"), "video/mp4")})
    assert "End the clip with" not in authed.get(f"/generations/{gid}/by-hand").text  # the last clip
    assert "everyone in the next scene still in frame" in genres.plan_rules("kids", 2, flow=True)


def test_one_clip_flow_brief_goes_to_flow_as_written(app, authed, drive_connected, net):
    """The buffalo sarpanch brief: a full 8-second prompt (layout, timed beats, dialogue lock, sound timeline) is
    better than Gemini's one-paragraph rewrite of it, so one clip from a long prompt goes to Flow as written."""
    from app import script
    from tests.test_app import by_hand
    beats = "".join(f"Beat {i}: Chacha does thing number {i} while the buffalo watches calmly. " for i in range(1, 9))
    brief = ("SECTION 4: Layout. The charpai is in the centre, the door on the right, the entrance on the left. "
             "Key light from the upper left. " * 6 + beats + 'Chacha says: "اوئے! میری پگ کدھر لے کے چلی ایں؟" '
             + "Sound: a booming moo at 3.6 seconds. " * 10 + "x" * 14500)
    assert len(brief) > 16000
    gid = by_hand(authed, ["unused"]).json()["id"]  # a by-hand video, to read the clip prompt off its page
    with app.state.db.session() as s:
        from app.models import Generation
        g = s.get(Generation, gid)
        g.prompt, g.planned, g.review = brief, True, False
    prompt = app.state.service.hand_scene(gid, 1)["prompt"]
    assert prompt.startswith(brief.strip()) and len(prompt) <= script.FLOW_INPUT_LIMIT
    assert "Shot: one continuous take" in prompt and "nobody speaks" not in prompt  # it has a quoted line
    with app.state.db.session() as s:
        s.get(Generation, gid).review = True  # edited on the script page: the script's prompt is sent
    assert not app.state.service.hand_scene(gid, 1)["prompt"].startswith(brief[:200])
    # the Flow screen takes a brief this long; the Studio keeps its 10,000
    ok = flow_idea(authed, prompt=brief, duration=8)
    assert ok.status_code == 200, ok.text
    studio = authed.post("/generations", data={"prompt": brief, "duration": "10", "aspect": "9:16"})
    assert studio.status_code == 400 and "under 10,000 characters" in studio.json()["detail"]
    assert 'maxlength="30000"' in authed.get("/flow").text and 'id="length-note"' in authed.get("/flow").text and 'maxlength="10000"' in authed.get("/").text
    # Gemini's script dropping a line doesn't stop it for review: the brief, with every line, is what Flow gets
    gid = ok.json()["id"]
    net.worker.jobs[gen(app, gid).worker_job_id].update(status="done", scenes=[{"action": "Chacha shouts."}])
    tick(app)
    assert gen(app, gid).status != GenStatus.REVIEW


def test_a_briefs_beats_timed_within_its_length_need_no_more_seconds():
    """The buffalo brief at 8 seconds was told "17 moments … Choose 72 seconds": its beats and timed lines all fall
    within 0–8 seconds, so they fit; more seconds would only push every beat into the first clip."""
    from app.checks import prompt_notes
    beats = "".join(f"Beat {i} — moment: {i - 1}.00 to {i}.00 seconds.\n" for i in range(1, 9))
    timed = "".join(f"- {i}.10–{i}.50 seconds: a sound.\n" for i in range(8))
    brief = "Scene 1: Chacha sips. Scene 2: the buffalo walks in. Scene 3: he shouts.\n" + beats + timed
    brief += "SECTION 4: Layout. The charpai is in the centre, the door on the right. " * 25  # a full brief
    assert not any("moments" in n["text"] for n in prompt_notes(brief, [], seconds=8, clip=8))
    # untimed moments, or times past the length, still say the story needs more time
    many = " ".join(f"Scene {i}: something happens." for i in range(1, 8))
    assert any("moments" in n["text"] for n in prompt_notes(many, [], seconds=8, clip=8))
    assert any("moments" in n["text"] for n in prompt_notes(brief + "\nBeat 9 — more: 8.00 to 24.00 seconds.\n",
                                                             [], seconds=8, clip=8))


def test_a_16_second_brief_with_numbered_beat_headings_splits_and_the_note_says_how_it_goes():
    """The superstar goat brief: "16. OPENING: 0–2 SECONDS" … "23. FINAL PUNCHLINE: 14–16 SECONDS" went to both
    clips as shared text. They're its beats; and the length note judges each clip, not the whole 23,876."""
    from app.checks import prompt_notes
    from app.timeline import brief_clips
    shared = "4. MAIN CHARACTER: CHACHA BASHIRA\n" + "A round village uncle with a grey moustache. " * 90
    beats = ("15. ABSOLUTE TIMELINE\n0–2 sec\tChacha prepares to sing\n8–10 sec\tCrowd cheers\n"
             "16. OPENING: 0–2 SECONDS\nChacha holds the microphone.\n"
             "20. CROWD REACTION: 8–10 SECONDS\nGuests clap.\n"
             "23. FINAL PUNCHLINE: 14–16 SECONDS\nChacha says the punchline.\n")
    brief = shared + "\n" + beats
    first, second = brief_clips(brief, 2, 8)
    assert "OPENING: 0–2" in first and "FINAL PUNCHLINE" not in first and "MAIN CHARACTER" in first
    assert "FINAL PUNCHLINE: 14–16" in second and "OPENING" not in second and "MAIN CHARACTER" in second
    notes = lambda text, s=16: [n["text"] for n in prompt_notes(text, [], seconds=s, clip=8)]  # noqa: E731
    assert not any("sent as written" in n or "characters" in n for n in notes(brief))
    untimed = shared + "\n16. OPENING: 0–2 SECONDS\nChacha holds the microphone.\n"  # nothing for clip 2
    assert any("Not sent as written" in n for n in notes(untimed))
    huge = "4. MAIN CHARACTER\n" + "A round village uncle with a grey moustache. " * 520 + "\n" + beats
    assert any(n.startswith("Clip ") and "may refuse it" in n for n in notes(huge))


GARDEN_BRIEF = ("A 16-second vertical 9:16 3D animated family cartoon.\n\n"
                "MAIN CHARACTERS:\n\nPHUPPO: A Pakistani woman in her early 30s in a blue shalwar kameez. " + "Warm and cheerful. " * 140
                + "\n\nSETTING: A colourful garden at golden hour, key light from the upper left.\n\n"
                "SCENE 1 — 0–4 SECONDS:\n\nCamera: tracking forward.\n\nPhuppo walks; the child points at Bunny.\n\n"
                "SCENE 2 — 4–8 SECONDS:\n\nCamera: low-angle tracking following Bunny.\n\nBunny hops between flowers.\n\n"
                "SCENE 3 — 8–12 SECONDS:\n\nCamera: side tracking.\n\nBunny zigzags round a flower bed.\n\n"
                "SCENE 4 — 12–16 SECONDS:\n\nCamera: slow pull-back.\n\nAll three stop together, smiling.\n\n"
                "ANIMATION: Smooth, fluid cartoon animation with squash and stretch.\n\n"
                "AUDIO: Birds singing, soft hopping, the child giggling. No dialogue.")


def test_a_brief_near_flows_limit_goes_without_our_rules():
    """The buffalo brief is about 22,500 characters: the box takes it whole, and it goes to Flow without our rules
    block (it has its own), to stay near the 22,800 Flow was seen to make a video from."""
    from app import script
    brief = "SECTION 1: Chacha sips lassi. " * 760
    assert 22000 < len(brief) < 23000
    sent = script.as_written_prompt(brief, ["Chacha"], {"characters": [{"name": "Chacha"}]}, "3D cartoon", True)
    assert sent.startswith(brief.strip()) and "Reference images: 1 = Chacha" in sent and "People:" not in sent
    # video 164: the essentials go with every brief, even this long (entrance, places, no camera jumps)
    for line in script.BRIEF_ESSENTIALS:
        assert line in sent
    short = script.as_written_prompt(brief[:5000], [], {"characters": [{"name": "Chacha"}]}, "3D cartoon", True)
    assert "People: exactly 1 main character" in short and short.count("Shot:") == 1
    assert "Start: the first frame shows only what the first beat describes" in short


def test_longer_flow_brief_is_split_by_time_and_sent_as_written(app, authed, drive_connected, net, tmp_path):
    """A full brief for a 16-second video (#159's garden): every clip gets its shared sections word for word, which
    part of the story it is, and only its own timed scenes."""
    from tests.test_app import by_hand
    assert len(GARDEN_BRIEF) > 3000
    gid = by_hand(authed, ["one", "two"]).json()["id"]
    with app.state.db.session() as s:
        from app.models import Generation
        g = s.get(Generation, gid)
        g.prompt, g.planned, g.review = GARDEN_BRIEF, True, False
    first = app.state.service.hand_scene(gid, 1)["prompt"]
    for shared in ("PHUPPO: A Pakistani woman", "SETTING: A colourful garden", "ANIMATION: Smooth, fluid",
                   "AUDIO: Birds singing"):
        assert shared in first
    assert "THIS CLIP: part 1 of 2, story seconds 0–8" in first
    assert "SCENE 2 — 4–8 SECONDS" in first and "SCENE 3 — 8–12" not in first and "Shot: one continuous" in first
    authed.post(f"/generations/{gid}/scenes/1/clip", files={"clip": ("x.mp4", clip_bytes(tmp_path, "s1"), "video/mp4")})
    second = app.state.service.hand_scene(gid, 1)["prompt"]
    assert "THIS CLIP: part 2 of 2, story seconds 8–16" in second and "SETTING: A colourful garden" in second
    assert "SCENE 4 — 12–16 SECONDS" in second and "SCENE 1 — 0–4" not in second
    # no time for the second clip: it can't be split, so the clips are made from the script as before
    with app.state.db.session() as s:
        s.get(Generation, gid).prompt = GARDEN_BRIEF.split("SCENE 3")[0]
    assert "THIS CLIP" not in app.state.service.hand_scene(gid, 1)["prompt"]


def test_flow_clip_with_no_lines_birdsong_and_an_animal():
    """Video 159: a clip with no lines said nothing about speech, "birds singing" dropped "no music", and the
    bunny was told "same person, hair, outfit"."""
    from app import script
    cast = [{"id": 9, "name": "Bunny", "appearance": "small fluffy white rabbit", "image_path": "bunny.png"},
            {"id": 1, "name": "Phuppo", "image_path": "phuppo.png"}]
    scene = {"characters": [{"id": 1, "name": "Phuppo", "position": "frame left"},
                            {"id": 9, "name": "Bunny", "position": "frame right"}],
             "action": "Bunny hops between the flowers. Phuppo laughs.", "sound": "Birds singing, soft breeze."}
    flow = script.compile_prompt(scene, cast, style="Pixar-style 3D cartoon", flow=True, music=False,
                                 room=script.FLOW_MAX_PROMPT)
    assert "Speech: nobody speaks; laughs and giggles are fine." in flow and "no music." in flow
    assert "Bunny, frame right: the same animal as reference image 2, same face, fur, colours and size" in flow
    assert "Phuppo, frame left, exactly as in reference image 1 (same face, hair, outfit and style)" in flow
    sung = dict(scene, sound="A woman singing a lullaby.")
    assert "no music" not in script.compile_prompt(sung, cast, flow=True, music=False, room=script.FLOW_MAX_PROMPT)
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
    assert "Everyone keeps their side of the frame" in genres.plan_rules("comedy", 3, flow=True)
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
    for rule in ("one calm move", "which hand holds what", "continues the last",
                 "everyday Pakistani Punjabi in Shahmukhi", "never more than 20", "effects only",
                 "no comedy or Pixar style unless asked", "steering wheel in front of the driver",
                 "Nobody speaks while eating or laughing", "clearly different voices"):
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
    assert ('Ammi walks in on the right and jumps back. Ammi (frame right) screams: "ہائے ربا! بھوت!" (Bahu silent, '
            'lips closed). Bahu (frame left), not moving, says calmly: "امی جی، میں آں!" (Ammi silent, lips closed).'
            ) in first.prompt
    assert "Voices: Ammi (frame right): older woman, loud and dramatic; Bahu (frame left): young woman, calm and " \
           "cheeky, Pakistani Punjabi accent." in first.prompt
    assert "Urdu accent" not in first.prompt and "Medium shot," not in first.prompt
    assert "camera completely still" in first.prompt  # "Camera: … static"
    assert "only the move above" in second.prompt  # no camera written: Gemini's, as it moves


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
