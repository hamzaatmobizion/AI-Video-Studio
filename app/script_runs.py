"""The AutoFlow tab: each script saved in the scripts folder (SCRIPTS_DIR) made into a Flow by-hand video and given to
the hand bot, one video at a time, until every script is made.

A run makes the next script's video only once the one before is finished: made (READY), or stopped (the bot stopped
on it, it failed, or it waits for review), which is skipped and left for the user to finish. Every video gets the Flow
form's defaults: by hand, no review, Check each clip off, Auto style, language and genre, no characters, 9:16 unless
the script says 16:9. A script is made once a video from it is READY; a new run skips it.
"""
import json
import logging
import re
import threading
import zipfile
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree

from sqlalchemy import select

from app.enums import ACTIVE, SCENE_SECONDS, GenStatus, Provider
from app.models import Generation, utcnow
from app.routes.api import _CLOCK, _LENGTH, _WORDS, MAX_IDEA_FLOW, MAX_SCENES, duration_from_prompt
from app.script import aspect_from_prompt
from app.timeline import clip_numbers
from app.services.generation import UserError

log = logging.getLogger("app.script_runs")
logging.getLogger("pypdf").setLevel(logging.ERROR)  # "Ignoring wrong pointing object…": harmless, read every 15 s
STEP = SCENE_SECONDS[Provider.FLOW]
SUFFIXES = (".md", ".txt", ".pdf", ".docx")
MADE = {GenStatus.READY, GenStatus.SAVE_FAILED}  # SAVE_FAILED: the video is made, only its Drive upload failed
# "Total Duration: 24 Seconds", "Video Duration: 24 Seconds": the video's length (other lengths in the story don't count)
TOTAL = re.compile(r"^\W*(?:total|video)\s+duration\s*:\s*(.+)$", re.I | re.M)
STUCK = timedelta(minutes=60)  # a clip takes the bot up to 15 minutes, 3 tries at most: no upload in this long is stuck
# The bot's stop that's about this script's own clip (Flow refused it every time); any other stop (a CAPTCHA, a flag,
# signed out, payment, a button it can't see) would stop it again on the next script, so the run stops
CONTENT_STOP = re.compile(r"^Flow failed the clip \d+ times", re.I)
HEADERS = re.compile(r"^(?:complete ai video script|video production script|ai video script)$", re.I)
LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"})
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"  # Word's XML namespace


def docx_text(path: Path) -> str:
    """A Word (.docx) file's words, one paragraph a line (a table's cells too, each on its line). Only the words:
    formatting, pictures and layout are left out."""
    try:
        with zipfile.ZipFile(path) as z:
            root = ElementTree.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        raise ValueError("it isn't a Word .docx file (an old .doc? Save it as .docx)") from None
    lines = []
    for p in root.iter(f"{W}p"):
        parts = []
        for el in p.iter():
            if el.tag == f"{W}t":
                parts.append(el.text or "")
            elif el.tag == f"{W}tab":
                parts.append("\t")
            elif el.tag in (f"{W}br", f"{W}cr"):
                parts.append("\n")
        lines.append("".join(parts))
    return "\n".join(lines)


def pdf_text(path: Path) -> str:
    """A PDF's words, page after page. A scanned PDF (pictures of pages) has none."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("the PDF has a password")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        return text.translate(LIGATURES)  # "ﬂoor" → "floor": a PDF keeps fi/fl as one joined letter
    except ValueError:
        raise
    except (PdfReadError, Exception) as e:  # noqa: BLE001 — pypdf raises many kinds on a broken file
        raise ValueError(f"it isn't a readable PDF ({e})") from None


@lru_cache(maxsize=256)
def _text_of(path: Path, mtime_ns: int, size: int) -> str:
    """The script's text, read again only when the file changes (the page reloads every 15 s during a run)."""
    kind = path.suffix.lower()
    if kind == ".pdf":
        return pdf_text(path)
    if kind == ".docx":
        return docx_text(path)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValueError("it isn't plain text (UTF-8)") from None


def in_progress(gen: Generation) -> bool:
    """Still being made: Gemini writes its clips, the app joins or saves them, or the bot is making a clip."""
    return gen.status in ACTIVE or (gen.status == GenStatus.HAND and gen.bot_at is not None and not gen.bot_note)


def _length(num: str, unit: str) -> float:
    """Seconds in "24 Seconds", "1.5 minutes", "two min" (as duration_from_prompt reads them)."""
    n = _WORDS.get(" ".join(num.lower().split()))
    n = float(num) if n is None else n
    return n * 60 if unit.lower().startswith("m") else n


def title_of(text: str, name: str) -> str:
    """The script's title: its "Title: …" line, else its first line that isn't a "… SCRIPT" header."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines[:5]:
        if ln.lower().startswith("title:"):
            return ln.split(":", 1)[1].strip() or name
    first = next((ln for ln in lines if not HEADERS.match(ln)), "")
    return first.lstrip("# ").strip()[:120] or name


class ScriptRuns:
    def __init__(self, service, settings, bot=None):
        self.service, self.settings, self.bot = service, settings, bot  # bot: app.bot_process.BotProcess
        self.lock = threading.Lock()
        self._stop_after_restart()

    def _stop_after_restart(self) -> None:
        """A run going when the app quit doesn't carry on by itself (the bot would start moving the mouse as soon as
        the app starts): it waits for Start."""
        state = self._load()
        stopped = {uid: {"started": None, "error": "The app was restarted during the run, so it stopped. Press Start "
                                                   "to carry on."} for uid, run in state.items() if run.get("started")}
        if stopped:
            self._save({**state, **stopped})

    # ---- the folder ------------------------------------------------------
    @property
    def folder(self) -> Path:
        return Path(self.settings.SCRIPTS_DIR)

    def files(self) -> list[Path]:
        """The scripts, by name (01-…, 02-…); a name starting with "_" or "." (the template), or "~" (the lock file
        Word keeps beside an open document), isn't one."""
        if not self.folder.is_dir():
            return []
        return sorted(p for p in self.folder.iterdir()
                      if p.is_file() and p.suffix.lower() in SUFFIXES and not p.name.startswith(("_", ".", "~")))

    @staticmethod
    def read(path: Path) -> dict:
        """One script: its text, title, length and clips, or the problem that stops it being made."""
        try:
            stat = path.stat()
            text = _text_of(path, stat.st_mtime_ns, stat.st_size)
            # a PDF pads each line with spaces to the page's width; 3+ empty lines are a page break
            text = re.sub(r"\n{3,}", "\n\n", "\n".join(ln.rstrip() for ln in text.splitlines())).strip()
        except (OSError, ValueError) as e:
            return {"name": path.name, "title": path.name, "text": "", "seconds": 0, "clips": 0,
                    "problem": f"Can't read the file: {e}", "as_written": False}
        # the length its "Total Duration" line gives; only without one, the longest length the script mentions
        total = TOTAL.search(text)
        said = total.group(1) if total else text
        seconds = duration_from_prompt(said, STEP) or 0
        clips, numbers, most = seconds // STEP, clip_numbers(text), MAX_SCENES * STEP
        longest = max([*(_length(n, u) for n, u in _LENGTH.findall(said)),
                       *(int(m) * 60 + int(s) for m, s in _CLOCK.findall(said))], default=0)
        empty = ("No text found in the PDF: a scanned PDF is a picture of the page, with no words to read. Copy its "
                 "text into a .md file instead." if path.suffix.lower() == ".pdf" else "The file is empty.")
        problem = (empty if not text
                   else f"It's longer than {MAX_IDEA_FLOW:,} characters." if len(text) > MAX_IDEA_FLOW
                   else 'Write its length, such as "Total Duration: 24 Seconds".' if not seconds
                   else f"It's {longest:g} seconds long: a video is at most {MAX_SCENES} clips "
                        f"({most // 60}:{most % 60:02d})." if longest > most
                   else f"Its clips are numbered {', '.join(map(str, numbers))}: number them CLIP 1, CLIP 2, … with "
                        f"none missing." if numbers and numbers != list(range(1, len(numbers) + 1))
                   else f"It says {seconds} seconds ({clips} clip{'' if clips == 1 else 's'}) but has {len(numbers)} "
                        f"CLIP block{'' if len(numbers) == 1 else 's'}: add or remove a clip, or make its Total "
                        f"Duration {len(numbers) * STEP} seconds." if numbers and len(numbers) != clips
                   else "")
        # laid out in clips that match its length: each clip goes to Flow as written, with no Gemini and no app rules
        return {"name": path.name, "title": title_of(text, path.name), "text": text, "seconds": seconds,
                "clips": clips, "problem": problem, "as_written": bool(numbers) and not problem}

    # ---- the run's state (var/script_runs.json: {user id: {"started": iso time, "error": ""}}) ---------------
    @property
    def state_path(self) -> Path:
        return Path(self.settings.VAR_DIR) / "script_runs.json"

    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state))

    def run_of(self, user_id: int) -> dict | None:
        return self._load().get(str(user_id))

    def start(self, user_id: int) -> None:
        if not self.settings.bot_configured:
            raise UserError("The hand bot isn't set up: BOT_API_KEY and BOT_USER_EMAIL aren't set.")
        if not any(not s["problem"] and s["state"] != "made" for s in self.scripts(user_id)):
            raise UserError("There's no script left to make. Paste a new one into the scripts folder.")
        with self.lock:
            state = self._load()
            state[str(user_id)] = {"started": utcnow().isoformat(), "error": ""}
            self._save(state)
        log.info("user %s: script run started", user_id)
        if self.bot:
            self.bot.ensure(force=True)  # no terminal needed: the app starts the hand bot
        self.step(user_id)

    def stop(self, user_id: int, error: str = "") -> None:
        """No new script is started; a video already given to the bot is still made."""
        with self.lock:
            state = self._load()
            if error:
                state[str(user_id)] = {"started": None, "error": error}
            else:
                state.pop(str(user_id), None)
            self._save(state)
        log.info("user %s: script run stopped%s", user_id, f": {error}" if error else "")

    # ---- what's made -----------------------------------------------------
    def _videos(self, user_id: int) -> dict[str, list[Generation]]:
        """The user's videos made from each script file, newest first."""
        with self.service.db.session() as s:
            rows = s.scalars(select(Generation).where(Generation.user_id == user_id,
                                                      Generation.script_file.is_not(None))
                             .order_by(Generation.id.desc())).all()
        out: dict[str, list[Generation]] = {}
        for g in rows:
            out.setdefault(g.script_file, []).append(g)
        return out

    def scripts(self, user_id: int) -> list[dict]:
        """Every script with where it is: made, making (now), stopped (skipped this run), waiting or a problem."""
        run = self.run_of(user_id) or {}
        started = datetime.fromisoformat(run["started"]) if run.get("started") else None
        unapproved = run.get("unapproved") or {}
        videos = self._videos(user_id)
        out = []
        for path in self.files():
            sc = self.read(path)
            gens = videos.get(path.name, [])
            made = next((g for g in gens if g.status in MADE), None)
            latest = gens[0] if gens else None
            # a script stopped for review is approved by the run (step), unless that failed
            review = latest is not None and latest.status == GenStatus.REVIEW and started is not None
            if made:
                sc.update(state="made", gen=made)
            elif latest and (in_progress(latest) or (review and str(latest.id) not in unapproved)):
                sc.update(state="making", gen=latest)
            elif review and str(latest.id) in unapproved:
                sc.update(state="stopped", gen=latest, note=f"its script couldn't be approved: "
                                                            f"{unapproved[str(latest.id)]}")
            elif latest and started and latest.created_at >= started:
                sc.update(state="stopped", gen=latest)  # tried in this run: skipped
            elif sc["problem"]:
                sc.update(state="problem", gen=latest)
            else:
                sc.update(state="waiting", gen=latest)  # latest: an earlier try that didn't finish
            out.append(sc)
        return out

    def _halt(self, user_id: int, error: str) -> None:
        """Stops the run, saying why on the page (the caller holds the lock)."""
        state = self._load()
        state[str(user_id)] = {"started": None, "error": error}
        self._save(state)
        log.info("user %s: script run stopped: %s", user_id, error)

    def _approve(self, user_id: int, gen: Generation) -> None:
        """Approves the script Gemini wrote, as it is, the way Approve on its script page does (a Flow video stops for
        review when the script left out a line the prompt wrote). If it can't be (a scene left empty, a clip prompt
        too long), the video is skipped with why."""
        try:
            self.service.save_script(gen.id, user_id, [sc.script() for sc in gen.scenes], gen.style or "",
                                     gen.language or "", approve=True)
            log.info("user %s: generation %s's script approved by the script run", user_id, gen.id)
        except UserError as e:
            state = self._load()
            state.setdefault(str(user_id), {}).setdefault("unapproved", {})[str(gen.id)] = e.message
            self._save(state)
            log.info("user %s: generation %s's script couldn't be approved: %s", user_id, gen.id, e.message)

    # ---- the scheduler ---------------------------------------------------
    def tick(self) -> None:
        for uid, run in list(self._load().items()):
            if run.get("started"):
                try:
                    if self.bot:
                        self.bot.ensure()  # started again if it exited (at most every 5 minutes)
                    self.step(int(uid))
                except Exception:  # noqa: BLE001 — one user's run must not stop the scheduler
                    log.exception("user %s: script run step failed", uid)

    def step(self, user_id: int) -> int | None:
        """Gives the bot the next script once no video of this run is still being made; ends the run after the last.
        The new video's id, or None."""
        with self.lock:
            run = self.run_of(user_id)
            if not run or not run.get("started"):
                return None
            scripts = self.scripts(user_id)
            started = datetime.fromisoformat(run["started"])
            for sc in scripts:
                g = sc.get("gen")
                if sc["state"] == "making" and g.status == GenStatus.REVIEW:
                    self._approve(user_id, g)
                elif sc["state"] == "making" and g.status == GenStatus.HAND and utcnow() - g.updated_at > STUCK:
                    note = (f"No clip from the bot in {STUCK.seconds // 60} minutes: it may be stuck. Check Chrome, "
                            "then press Try again.")
                    self.service.bot_stopped(g.id, user_id, note)
                    return self._halt(user_id, f"{sc['name']} (video #{g.id}): {note}")
                elif (sc["state"] == "stopped" and g.bot_note and g.created_at >= started
                      and not CONTENT_STOP.match(g.bot_note)):
                    # a CAPTCHA, a flag, signed out…: the next script would only stop the same way
                    return self._halt(user_id, f"The bot stopped on {sc['name']} (video #{g.id}): {g.bot_note} Sort "
                                               "it out in Chrome, press Try again on that video, then Start to carry "
                                               "on with the rest.")
            if any(s["state"] == "making" for s in self.scripts(user_id)):
                return None
            nxt = next((s for s in scripts if s["state"] == "waiting"), None)
            if nxt is None:
                state = self._load()
                state.pop(str(user_id), None)
                self._save(state)
                log.info("user %s: script run finished", user_id)
                return None
            try:
                # given to the bot now: it takes the video once its clips are ready (status HAND)
                gen_id = self.service.create(user_id, [nxt["text"]], aspect_from_prompt(nxt["text"]) or "9:16", [],
                                             plan=nxt["clips"], provider=Provider.FLOW, by_hand=True,
                                             script_file=nxt["name"], bot=True)
            except UserError as e:
                return self._halt(user_id, f"{nxt['name']}: {e.message}")
        log.info("user %s: script %s is generation %s, given to the hand bot", user_id, nxt["name"], gen_id)
        return gen_id
