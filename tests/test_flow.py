"""The Flow screen: the same Studio, with every clip made in Google Flow (Veo 3.1 - Lite, Flow credits, 8 s scenes)."""
import json

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
    # Flow starts the clip exactly from the drawn frame (Frames mode takes only that picture), so the prompt describes
    # Ali in words, never as "reference image 1", and names the frame as picture 1
    assert first["path"] == "/videos" and first["images"] == 1
    assert b'name="provider"\r\n\r\nflow' in first["content"] and b'name="frame"\r\n\r\n0' in first["content"]
    assert b"Reference image 1 is this shot's first frame" in first["content"]
    assert b"as reference image" not in first["content"] and b"man in his 30s, beard" in first["content"]
    assert gen(app, gid).scenes[0].still_path.endswith("scene-1-still.png")
    finish(net, app, "job3")
    second = net.worker.submits[3]
    assert second["path"] == "/videos" and second["images"] == 1  # no second still: scene 1's last frame alone
    assert b'name="frame"\r\n\r\n0' in second["content"]
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
    net.worker.pauses["flow"] = OUT_OF_CREDITS
    net.worker.jobs["job1"].update(status="failed", error=OUT_OF_CREDITS["message"])
    tick(app)
    g = gen(app, gid)
    assert g.status == GenStatus.PAUSED and "out of credits" in g.error and "Flow credits" in g.error
    net.worker.clips["job2"] = clip_bytes(tmp_path, "job2")
    assert authed.post(f"/generations/{gid}/resume").status_code == 200
    assert net.worker.resumed_sites == ["flow"]
    finish(net, app, "job2")
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
