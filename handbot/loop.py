"""The bot's loop: take the waiting by-hand scene from the app, make its clip in Flow, upload it, rest, repeat."""
import json
import logging
import random
import subprocess
import tempfile
import time
from pathlib import Path

from handbot.app_client import AppClient, AppError
from handbot.config import BotSettings
from handbot.flow import FlowHand, Stop
from handbot.screen import Screen, notify

log = logging.getLogger("handbot")


class Pace:
    """The worker's Flow pacing (worker/pipeline.py): a rest of MIN_GAP_SECONDS × 1–1.5 after each clip, and at most
    MAX_PER_HOUR clips started in any hour. Kept on disk so a restart doesn't reset it."""

    def __init__(self, settings: BotSettings):
        self.s, self.path = settings, settings.pace_path

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"starts": [], "next_at": 0}

    def wait_seconds(self) -> float:
        d, now = self._load(), time.time()
        recent = sorted(t for t in d["starts"] if t > now - 3600)
        wait = max(0.0, d["next_at"] - now)
        if len(recent) >= self.s.MAX_PER_HOUR:
            wait = max(wait, recent[-self.s.MAX_PER_HOUR] + 3600 - now)
        return wait

    def started(self) -> None:
        d, now = self._load(), time.time()
        d["starts"] = [t for t in d["starts"] if t > now - 3600] + [now]
        self.path.write_text(json.dumps(d))

    def finished(self) -> None:
        d = self._load()
        d["next_at"] = time.time() + self.s.MIN_GAP_SECONDS * random.uniform(1.0, 1.5)
        self.path.write_text(json.dumps(d))


def run(settings: BotSettings) -> None:
    settings.ensure_dirs()
    app, pace, hand = AppClient(settings), Pace(settings), FlowHand(settings, Screen(settings))
    log.info("hand bot started; asking %s for clips every %ds", settings.app_url, settings.POLL_SECONDS)
    while True:
        try:
            job = app.next_scene()
        except (AppError, OSError) as e:
            log.warning("can't reach the app: %s", e)
            time.sleep(settings.POLL_SECONDS)
            continue
        if job is None:
            time.sleep(settings.POLL_SECONDS)
            continue
        wait = pace.wait_seconds()
        if wait > 0:
            log.info("resting %ds before the next clip", wait)
            time.sleep(wait)
            continue  # the scene may have been cancelled meanwhile
        _one(settings, app, pace, hand, job)


def _one(settings: BotSettings, app: AppClient, pace: Pace, hand: FlowHand, job: dict) -> None:
    gen, pos = job["gen_id"], job["position"]
    log.info("video %s scene %s: %d picture(s), %s", gen, pos, len(job["images"]), job["aspect"])
    with tempfile.TemporaryDirectory(prefix="handbot-") as tmp:
        images = app.images(job, Path(tmp))
        pace.started()
        try:
            clip = hand.make_clip(gen, job["prompt"], job["aspect"], images,
                                   settings.clips_dir / f"{gen}-scene-{pos}.mp4")
        except Stop as e:
            log.error("stopped: %s%s", e.message, f"  (screenshot: {e.shot})" if e.shot else "")
            _tell_app(settings, app, hand, job, e.message)
            return
        finally:
            pace.finished()
    try:
        app.upload(job, clip)
    except AppError as e:
        log.error("the clip is saved at %s but the upload failed: %s", clip, e)
        input("Upload it by hand on the app's by-hand page, then press Enter to carry on (Ctrl+C quits)… ")
        return
    log.info("video %s scene %s uploaded (%s)", gen, pos, clip)
    try:  # show it: back to your app's tab, scrolled down to the video
        hand.show_app(settings.app_url)
    except (Stop, OSError, subprocess.SubprocessError) as e:
        log.warning("couldn't go back to your app in Chrome: %s", getattr(e, "message", e))


def _tell_app(settings: BotSettings, app: AppClient, hand: FlowHand, job: dict, message: str) -> None:
    """Show why the bot stopped in your app (the video's page and card, with "Try again") and switch Chrome to your
    app. The bot leaves that video alone until "Try again", and carries on with the others."""
    try:
        notify(f"The bot stopped on video #{job['gen_id']}", message)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        app.stopped(job, message)
    except (AppError, OSError) as e:
        log.warning("couldn't tell the app the bot stopped: %s", e)
        input("Sort it out in Chrome, then press Enter to carry on (Ctrl+C quits)… ")
        return
    try:
        hand.show_app(settings.app_url)
    except (Stop, OSError, subprocess.SubprocessError) as e:
        log.warning("couldn't go back to your app in Chrome: %s", getattr(e, "message", e))
