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

    set_gen(app, first.id, status=GenStatus.HAND, bot_note="A CAPTCHA appeared.")  # the bot stopped: skipped
    second_id = runs.step(first.user_id)
    second = gens(app)[1]
    assert second.id == second_id and second.script_file == "02-boat-8s.md" and len(second.scenes) == 1
    page = authed.get("/autoflow").text
    assert "Skipped:" in page and "A CAPTCHA appeared." in page

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
