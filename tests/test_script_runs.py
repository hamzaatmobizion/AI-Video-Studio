import json
from pathlib import Path

import pytest

from app.enums import GenStatus
from app.models import Generation
from app.script_runs import title_of

BOT_KEY = "bot-key-1234567890123456789"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def folder(app, tmp_path):
    d = tmp_path / "scripts"
    d.mkdir()
    app.state.settings.SCRIPTS_DIR = d
    return d


@pytest.fixture
def bot(app):
    app.state.settings.BOT_API_KEY, app.state.settings.BOT_USER_EMAIL = BOT_KEY, "owner@example.com"


def script(seconds: int, title: str) -> str:
    return f"COMPLETE AI VIDEO SCRIPT\n{title}\n\nTotal Duration: {seconds} Seconds\n\nA boy flies a kite on a beach."


def gens(app) -> list[Generation]:
    with app.state.db.session() as s:
        return s.query(Generation).order_by(Generation.id).all()


def set_gen(app, gid, **fields):
    with app.state.db.session() as s:
        g = s.get(Generation, gid)
        for k, v in fields.items():
            setattr(g, k, v)


def test_title_and_length_are_read_from_the_saved_scripts():
    assert title_of("COMPLETE AI VIDEO SCRIPT\nTHE LUCKY COIN\n\nTotal Duration: 24 Seconds", "x.md") == "THE LUCKY COIN"
    assert title_of("VIDEO PRODUCTION SCRIPT\nTitle: THE POOR BOY\n", "x.md") == "THE POOR BOY"
    from app.script_runs import ScriptRuns
    for path, seconds in [("01-the-last-cookie-8s.md", 8), ("03-the-lucky-coin-24s.md", 24),
                          ("05-the-stray-dog-40s.md", 40)]:
        p = ROOT / "scripts" / path
        if p.exists():  # the scripts folder is only on this Mac (it's in .gitignore)
            sc = ScriptRuns.read(p)
            assert sc["seconds"] == seconds and sc["clips"] == seconds // 8 and not sc["problem"], sc


def test_scripts_page_lists_the_folder(authed, folder):
    (folder / "01-kite-16s.md").write_text(script(16, "THE KITE"))
    (folder / "02-empty.md").write_text("")
    (folder / "_TEMPLATE.md").write_text(script(8, "TEMPLATE"))
    (folder / "notes.png").write_bytes(b"x")
    page = authed.get("/autoflow").text
    assert "THE KITE" in page and "16 s · 2 clips" in page and "The file is empty." in page
    assert "TEMPLATE" not in page and "notes.png" not in page
    assert "hand bot isn't set up" in page


def test_start_needs_the_bot(authed, folder):
    (folder / "01-kite.md").write_text(script(8, "THE KITE"))
    r = authed.post("/autoflow/start")
    assert r.status_code == 400 and "hand bot isn't set up" in r.json()["detail"]


def test_run_makes_one_script_at_a_time_and_skips_a_stopped_one(app, authed, drive_connected, folder, bot):
    (folder / "01-kite-16s.md").write_text(script(16, "THE KITE"))
    (folder / "02-boat-8s.md").write_text(script(8, "THE BOAT"))
    (folder / "03-dog-24s.md").write_text(script(24, "THE DOG"))
    runs = app.state.script_runs
    assert authed.post("/autoflow/start").status_code == 200
    [first] = gens(app)
    assert first.script_file == "01-kite-16s.md" and first.by_hand and first.provider == "flow"
    assert first.bot_at is not None and len(first.scenes) == 2 and not first.review and not first.check_clips
    assert runs.step(first.user_id) is None and len(gens(app)) == 1  # still being made: the next one waits
    assert "Gemini is writing its clips" in authed.get("/autoflow").text

    # Flow refused this script's clip every time: only this script is skipped
    set_gen(app, first.id, status=GenStatus.HAND, bot_note="Flow failed the clip 3 times (refunded).")
    second_id = runs.step(first.user_id)
    second = gens(app)[1]
    assert second.id == second_id and second.script_file == "02-boat-8s.md" and len(second.scenes) == 1
    page = authed.get("/autoflow").text
    assert "Skipped:" in page and "Flow failed the clip 3 times" in page

    set_gen(app, second.id, status=GenStatus.READY)
    runs.step(first.user_id)
    third = gens(app)[2]
    assert third.script_file == "03-dog-24s.md" and len(third.scenes) == 3
    set_gen(app, third.id, status=GenStatus.READY)
    assert runs.step(first.user_id) is None
    assert runs.run_of(first.user_id) is None  # every script tried: the run ends
    assert len(gens(app)) == 3

    page = authed.get("/autoflow").text
    assert page.count("✓ Made") == 2
    # a new run makes again only the one that wasn't made
    assert authed.post("/autoflow/start").status_code == 200
    assert gens(app)[-1].script_file == "01-kite-16s.md" and len(gens(app)) == 4


def test_run_stops_when_a_video_cant_be_created(app, authed, folder, bot):
    (folder / "01-kite.md").write_text(script(8, "THE KITE"))  # Google Drive isn't connected
    assert authed.post("/autoflow/start").status_code == 200
    assert gens(app) == []
    state = json.loads((app.state.settings.VAR_DIR / "script_runs.json").read_text())
    assert "Connect Google Drive" in next(iter(state.values()))["error"]
    assert "The run stopped:" in authed.get("/autoflow").text


def test_stop_ends_the_run(app, authed, drive_connected, folder, bot):
    (folder / "01-kite.md").write_text(script(8, "THE KITE"))
    (folder / "02-boat.md").write_text(script(8, "THE BOAT"))
    authed.post("/autoflow/start")
    uid = gens(app)[0].user_id
    assert authed.post("/autoflow/stop").status_code == 200
    set_gen(app, gens(app)[0].id, status=GenStatus.READY)
    assert app.state.script_runs.step(uid) is None and len(gens(app)) == 1


# ---- PDF and Word files -------------------------------------------------------
def make_docx(path: Path, paragraphs: list[str]) -> None:
    import zipfile
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>')


def make_pdf(path: Path, lines: list[str]) -> None:
    """A one-page PDF with real text (not a picture), built by hand."""
    text = " ".join(f"({ln}) Tj 0 -20 Td" for ln in lines)
    stream = f"BT /F1 12 Tf 50 750 Td {text} ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))


def test_word_and_pdf_scripts_are_read(app, authed, drive_connected, folder, bot):
    make_docx(folder / "01-kite.docx", ["COMPLETE AI VIDEO SCRIPT", "THE KITE", "Total Duration: 16 Seconds",
                                        "A boy flies a kite on a beach."])
    make_pdf(folder / "02-boat.pdf", ["COMPLETE AI VIDEO SCRIPT", "THE BOAT", "Total Duration: 8 Seconds"])
    from pypdf import PdfWriter
    blank = PdfWriter()
    blank.add_blank_page(612, 792)
    blank.write(folder / "03-scan.pdf")
    (folder / "04-old.docx").write_bytes(b"not a zip")
    (folder / "~$01-kite.docx").write_bytes(b"word's lock file")
    page = authed.get("/autoflow").text
    assert "THE KITE" in page and "16 s · 2 clips" in page
    assert "THE BOAT" in page and "8 s · 1 clip" in page
    assert "No text found in the PDF" in page
    assert "isn&#39;t a Word .docx file" in page and "~$" not in page
    authed.post("/autoflow/start")
    first = gens(app)[0]
    assert first.script_file == "01-kite.docx" and "A boy flies a kite on a beach." in first.prompt
    set_gen(app, first.id, status=GenStatus.READY)
    app.state.script_runs.step(first.user_id)
    assert gens(app)[1].script_file == "02-boat.pdf" and "THE BOAT" in gens(app)[1].prompt


# ---- review is approved, the bot is started ------------------------------------
def test_a_script_stopped_for_review_is_approved(app, authed, drive_connected, folder, bot):
    (folder / "01-kite-16s.md").write_text(script(16, "THE KITE"))
    (folder / "02-boat-8s.md").write_text(script(8, "THE BOAT"))
    authed.post("/autoflow/start")
    first = gens(app)[0]
    with app.state.db.session() as s:  # Gemini wrote the script but left out a line: it stops for review
        g = s.get(Generation, first.id)
        g.status, g.review, g.planned = GenStatus.REVIEW, True, True
        for sc in g.scenes:
            sc.action, sc.prompt = "A boy flies a kite on a beach.", ""
    assert "Approving its script" in authed.get("/autoflow").text
    runs = app.state.script_runs
    assert runs.step(first.user_id) is None  # approved: its clips are made, the next script waits
    g = gens(app)[0]
    assert g.status == GenStatus.HAND and all(sc.prompt for sc in g.scenes) and g.bot_at is not None
    assert len(gens(app)) == 1


def test_a_script_that_cant_be_approved_is_skipped(app, authed, drive_connected, folder, bot):
    (folder / "01-kite-16s.md").write_text(script(16, "THE KITE"))
    (folder / "02-boat-8s.md").write_text(script(8, "THE BOAT"))
    authed.post("/autoflow/start")
    first = gens(app)[0]
    set_gen(app, first.id, status=GenStatus.REVIEW, review=True)  # its scenes are empty: Approve refuses
    runs = app.state.script_runs
    runs.step(first.user_id)
    assert gens(app)[1].script_file == "02-boat-8s.md"
    page = authed.get("/autoflow").text
    assert "Skipped:" in page and "couldn&#39;t be approved" in page and "Scene 1 is empty" in page


class FakeProc:
    pid, returncode = 4321, None

    def __init__(self, cmd, **kw):
        FakeProc.cmd, FakeProc.kw, self.stopped = cmd, kw, False
        FakeProc.last = self

    def poll(self):
        return self.returncode

    def terminate(self):
        self.stopped, self.returncode = True, -15

    def wait(self, timeout=None):
        return self.returncode


def test_start_starts_the_bot_once(app, authed, drive_connected, folder, bot, monkeypatch):
    import app.bot_process as bp
    monkeypatch.setattr(bp.subprocess, "Popen", FakeProc)
    app.state.settings.BOT_AUTOSTART = True
    (folder / "01-kite.md").write_text(script(8, "THE KITE"))
    assert "Start the bot" in authed.get("/autoflow").text
    authed.post("/autoflow/start")
    assert FakeProc.cmd[-3:] == ["-m", "handbot", "run"] and Path(FakeProc.kw["cwd"]) == ROOT
    first = FakeProc.last
    assert "The bot is running" in authed.get("/autoflow").text
    assert app.state.bot.ensure() is False and FakeProc.last is first  # running: not started twice
    first.returncode = 1  # it exited: its last words show
    (app.state.settings.VAR_DIR / "logs" / "handbot.log").write_text("✗ Screen Recording isn't allowed")
    page = authed.get("/autoflow").text
    assert "The bot exited" in page and "Screen Recording isn" in page
    assert app.state.bot.ensure() is False  # not again within 5 minutes on its own
    assert authed.post("/autoflow/bot").status_code == 200 and FakeProc.last is not first  # the button does
    app.state.bot.stop()
    assert FakeProc.last.stopped


def test_no_autostart_leaves_the_bot_alone(app, authed, folder, bot, monkeypatch):
    import app.bot_process as bp
    monkeypatch.setattr(bp.subprocess, "Popen", FakeProc)
    FakeProc.last = None
    assert authed.post("/autoflow/bot").status_code == 400
    assert FakeProc.last is None and "BOT_AUTOSTART is off" in authed.get("/autoflow").text


# ---- a script laid out in clips goes to Flow as written --------------------------
LAID_OUT = """COMPLETE AI VIDEO SCRIPT
THE KITE

Total Duration: 16 Seconds
Total Clips: 2
Style: Ultra-realistic cinematic storytelling
Genre: Emotional

CHARACTER DESCRIPTION AND CONSISTENCY
Main Character: Boy
Age: 10 years old
Red cap

CLIP 1: THE WIND

Time: 00:00–00:08

SCENE 1

Timestamp: 00:00–00:02

IMAGE PROMPT:

A boy in a red cap on a windy beach.

DIALOGUE:

Boy: "Look how high it flies!"

SOUND: Wind.

CLIP 2: THE FALL

Time: 00:08–00:16

SCENE 4

Timestamp: 00:08–00:11

IMAGE PROMPT:

The kite falls into the sea.

DIALOGUE:

Boy: "Oh no, my kite!"

ON-SCREEN TEXT:

NEVER GIVE UP.

COMPLETE VOICEOVER / DIALOGUE SCRIPT

Boy: "Look how high it flies!"
Boy: "Oh no, my kite!"

VIDEO EDITING INSTRUCTIONS
Add English subtitles.

FINAL TIMELINE
Clip	Scenes	Duration
Clip 1: The Wind	1, 2, 3	8 seconds
Clip 2: The Fall	4, 5, 6	8 seconds

END OF PRODUCTION SCRIPT
"""


def test_layout_clips_keeps_each_clip_and_leaves_the_editor_out():
    from app.timeline import layout_clips
    one, two = layout_clips(LAID_OUT, 2, 8)
    for clip in (one, two):
        assert clip.startswith("Style: Ultra-realistic cinematic storytelling\nGenre: Emotional")
        assert "CHARACTER DESCRIPTION AND CONSISTENCY" in clip and "Red cap" in clip
        assert "Total Duration" not in clip and "subtitles" not in clip and "FINAL TIMELINE" not in clip
        assert "COMPLETE VOICEOVER" not in clip and "END OF" not in clip
    assert "part 1 of 2, story seconds 0–8" in one and "Look how high" in one and "Oh no" not in one
    assert "part 2 of 2, story seconds 8–16" in two and "Oh no" in two and "Look how high" not in two
    assert "NEVER GIVE UP." in two and "NEVER GIVE UP." not in one
    assert layout_clips(LAID_OUT, 3, 8) is None  # its clips don't match the length
    assert layout_clips("A boy flies a kite. Total Duration: 16 Seconds", 2, 8) is None


def test_layout_first_style_final_screen_text_goes_in_the_last_clip():
    from app.timeline import layout_clips
    text = ("VIDEO PRODUCTION SCRIPT\nTitle: THE BOY\n\nMAIN CHARACTER CONSISTENCY\nCharacter: a boy\n\n"
            "CLIP 1 — THE STRUGGLE\nHe sits by the road.\n\nCLIP 2 — THE WORK\nHe studies.\n\n"
            "FINAL VIDEO EDITING INSTRUCTIONS\nUse cinematic transitions.\n\nFINAL SCREEN TEXT\n\n\"NEVER GIVE UP!\"\n\n"
            "END OF SCRIPT")
    one, two = layout_clips(text, 2, 8)
    assert "MAIN CHARACTER CONSISTENCY" in one and "He sits by the road." in one and "NEVER GIVE UP" not in one
    assert "He studies." in two and "FINAL SCREEN TEXT" in two and "NEVER GIVE UP!" in two
    assert "transitions" not in two


def test_a_laid_out_script_skips_gemini_and_goes_to_the_bot_as_written(app, authed, drive_connected, net, folder,
                                                                       bot):
    (folder / "01-kite-16s.md").write_text(LAID_OUT)
    headers = {"Authorization": f"Bearer {BOT_KEY}"}
    authed.post("/autoflow/start")
    g = gens(app)[0]
    assert not [x for x in net.worker.submits if x["path"] == "/plans"]  # Gemini wasn't asked
    assert g.status == GenStatus.HAND and g.bot_at is not None and len(g.scenes) == 2
    job = authed.get("/bot/next", headers=headers).json()
    assert job["gen_id"] == g.id and job["position"] == 1
    assert "CLIP 1: THE WIND" in job["prompt"] and "Look how high it flies!" in job["prompt"]
    assert "Red cap" in job["prompt"] and "Oh no, my kite!" not in job["prompt"]
    assert "English subtitles" not in job["prompt"]
    for rule in ("Start: the first frame", "Shot: one continuous take", "People:", "Look:", "no music"):
        assert rule not in job["prompt"]  # the script decides: none of the app's rules
    assert job["prompt"].rstrip().endswith('SOUND: Wind.')
    from tests.test_app import MP4
    r = authed.post(f"/bot/generations/{g.id}/scenes/1/clip", headers=headers, files={"clip": ("x.mp4", MP4, "video/mp4")})
    assert r.status_code == 200, r.text
    job = authed.get("/bot/next", headers=headers).json()
    assert job["position"] == 2 and "CLIP 2: THE FALL" in job["prompt"] and "NEVER GIVE UP." in job["prompt"]
    assert "Look how high it flies!" not in job["prompt"] and "Look:" not in job["prompt"]
    # (the test clip has no real last frame; with one, only the line saying to start from it is added)
    assert job["prompt"].rstrip().endswith("NEVER GIVE UP.")


# ---- a stop that would happen again, a stuck video, a restart --------------------
def test_a_captcha_stops_the_whole_run(app, authed, drive_connected, folder, bot):
    (folder / "01-kite.md").write_text(script(8, "THE KITE"))
    (folder / "02-boat.md").write_text(script(8, "THE BOAT"))
    authed.post("/autoflow/start")
    first = gens(app)[0]
    set_gen(app, first.id, status=GenStatus.HAND, bot_note="Google is showing a CAPTCHA.")
    runs = app.state.script_runs
    assert runs.step(first.user_id) is None and len(gens(app)) == 1  # the next script isn't tried
    run = runs.run_of(first.user_id)
    assert not run["started"] and "CAPTCHA" in run["error"] and "Try again" in run["error"]
    authed.post(f"/generations/{first.id}/bot")  # Try again, then Start: the run waits for it, then goes on
    authed.post("/autoflow/start")
    assert len(gens(app)) == 1
    set_gen(app, first.id, status=GenStatus.READY)
    runs.step(first.user_id)
    assert gens(app)[1].script_file == "02-boat.md"


def test_a_stuck_video_stops_the_run(app, authed, drive_connected, folder, bot):
    from datetime import timedelta
    from app.models import utcnow
    (folder / "01-kite.md").write_text(script(8, "THE KITE"))
    authed.post("/autoflow/start")
    first = gens(app)[0]
    set_gen(app, first.id, status=GenStatus.HAND)
    runs = app.state.script_runs
    assert runs.step(first.user_id) is None and runs.run_of(first.user_id)["started"]  # not stuck yet
    with app.state.db.session() as s:  # no clip for over an hour
        s.execute(Generation.__table__.update().where(Generation.id == first.id)
                  .values(updated_at=utcnow() - timedelta(minutes=61)))
    runs.step(first.user_id)
    assert "No clip from the bot in 60 minutes" in gens(app)[0].bot_note
    assert "No clip from the bot" in runs.run_of(first.user_id)["error"]


def test_a_run_doesnt_carry_on_after_the_app_restarts(app, authed, drive_connected, folder, bot):
    from app.script_runs import ScriptRuns
    (folder / "01-kite.md").write_text(script(8, "THE KITE"))
    authed.post("/autoflow/start")
    uid = gens(app)[0].user_id
    again = ScriptRuns(app.state.service, app.state.settings)  # what the app does when it starts
    run = again.run_of(uid)
    assert not run["started"] and "restarted" in run["error"]


# ---- what makes a script go as written -----------------------------------------
def test_total_duration_is_the_length_and_clips_must_match(folder):
    from app.script_runs import ScriptRuns
    clips = lambda n: "".join(f"CLIP {i}: PART\nScene text {i}.\n\n" for i in range(1, n + 1))  # noqa: E731
    cases = {
        "a.md": "Total Duration: 16 Seconds\nHe waited for 2 minutes.\n" + clips(2),
        "b.md": "Total Duration: 24 Seconds\n" + clips(2),
        "c.md": "Total Duration: 16 Seconds\nCLIP 1: A\nx\nCLIP 3: B\ny\n",
        "d.md": "Total Duration: 2 minutes\n" + clips(15),
        "e.md": "Total Duration: 16 Seconds\nCLIP ONE: A\nx\n\nClip Two — B\ny\n",
        "f.md": "A boy flies a kite for 16 seconds.",
    }
    for name, text in cases.items():
        (folder / name).write_text(text)
    got = {n: ScriptRuns.read(folder / n) for n in cases}
    assert got["a.md"]["seconds"] == 16 and got["a.md"]["as_written"] and not got["a.md"]["problem"]
    assert "says 24 seconds (3 clips) but has 2 CLIP blocks" in got["b.md"]["problem"]
    assert "make its Total Duration 16 seconds" in got["b.md"]["problem"] and not got["b.md"]["as_written"]
    assert "numbered 1, 3" in got["c.md"]["problem"]
    assert "120 seconds long: a video is at most 12 clips (1:36)" in got["d.md"]["problem"]
    assert got["e.md"]["as_written"] and got["e.md"]["clips"] == 2
    assert got["f.md"]["seconds"] == 16 and not got["f.md"]["as_written"] and not got["f.md"]["problem"]


def test_autoflow_clip_two_starts_from_the_last_frame(app, authed, drive_connected, folder, bot, tmp_path):
    from tests.fakes import PNG
    (folder / "01-kite-16s.md").write_text(LAID_OUT)
    authed.post("/autoflow/start")
    g = gens(app)[0]
    frame = tmp_path / "last.png"
    frame.write_bytes(PNG)
    svc = app.state.service
    svc._previous_frame = lambda gen, scene: frame if scene.position > 1 else None
    with app.state.db.session() as s:  # clip 1 is in
        s.get(Generation, g.id).scenes[0].clip_path = str(frame)
    hand = svc.hand_scene(g.id, g.user_id)
    assert hand["scene"].position == 2
    assert hand["mode"] == "frames" and [im["path"] for im in hand["images"]] == [frame]  # Flow's Start frame
    assert "CLIP 2: THE FALL" in hand["prompt"]
    assert hand["prompt"].rstrip().endswith("the action begins naturally from where everyone stands in it.")
    assert "Look:" not in hand["prompt"]  # still none of the app's rules
