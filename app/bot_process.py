"""The hand bot started by the app (AutoFlow's Start), so nobody has to run `python -m handbot run` in a terminal.

Only for a bot on this same computer (BOT_AUTOSTART, on by default): it's `python -m handbot run` from the project folder,
with its output in var/logs/handbot.log, and it stops with the app. A bot already running (started in a terminal, or
on another computer) is left alone: the app knows it by its asking for work (GenerationService.bot_online).

On macOS, the bot moves the mouse and takes screenshots, which macOS allows only to the app that started it: the terminal that
runs ./run.sh needs Accessibility and Screen Recording, as when the bot is started there by hand.
"""
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("app.bot_process")
ROOT = Path(__file__).resolve().parent.parent
RESTART_AFTER = 300  # seconds: a bot that exited is started again at most this often


class BotProcess:
    def __init__(self, service, settings):
        self.service, self.settings = service, settings
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0
        self.lock = threading.Lock()

    @property
    def log_path(self) -> Path:
        return Path(self.settings.VAR_DIR) / "logs" / "handbot.log"

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def running(self) -> bool:
        """Running: started by the app, or anywhere else (it asked for work in the last 2 minutes)."""
        return self.alive() or self.service.bot_online()

    def ensure(self, force: bool = False) -> bool:
        """Starts the bot unless it's running; one that exited is started again only after RESTART_AFTER, unless
        `force` (the Start button). Whether it was started now."""
        if not self.settings.BOT_AUTOSTART or not self.settings.bot_configured:
            return False
        with self.lock:
            if self.running() or (not force and time.monotonic() - self.started_at < RESTART_AFTER):
                return False
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            out = open(self.log_path, "w")  # noqa: SIM115 — the bot writes to it for as long as it runs
            self.proc = subprocess.Popen([sys.executable, "-m", "handbot", "run"], cwd=ROOT, stdout=out,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         env={**os.environ, "PYTHONUNBUFFERED": "1"})  # each line logged at once
            out.close()  # the bot has its own copy
            self.started_at = time.monotonic()
        log.info("hand bot started (pid %s), output in %s", self.proc.pid, self.log_path)
        return True

    def stop(self) -> None:
        """Stops a bot the app started (the app is shutting down)."""
        with self.lock:
            if not self.alive():
                return
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        log.info("hand bot stopped")

    def exited(self) -> str:
        """Why a bot the app started has exited: the end of its output; "" while it runs or if it never started."""
        if self.proc is None or self.proc.poll() is None:
            return ""
        try:
            lines = self.log_path.read_text(errors="replace").strip().splitlines()
        except OSError:
            lines = []
        return "\n".join(lines[-8:]) or f"It exited with code {self.proc.returncode}."
