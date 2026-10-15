"""Exclusive use of the browser profile, the manual sign-in window, and the session check."""
import fcntl
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable

from worker.config import WorkerSettings
from worker.jobs import JobStore

log = logging.getLogger("worker.session")


# Playwright's driver was killed mid-check, e.g. by Ctrl+C on ./run.sh.
INTERRUPTED = "The last check was interrupted because the worker was stopping. Click Check session."


class Busy(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ProfileLock:
    """Cross-process lock on the browser profile (flock), so generation, the session check and the
    sign-in window (from the server or the CLI) never open the profile at the same time."""

    def __init__(self, path):
        self.path = path
        self._fd: int | None = None
        self.holder: str | None = None

    def try_acquire(self, holder: str) -> bool:
        if self._fd is not None:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, holder.encode())
        self._fd, self.holder = fd, holder
        return True

    def release(self, holder: str) -> None:
        if self._fd is None or self.holder != holder:
            return
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd, self.holder = None, None

    def held_by_other_process(self) -> str | None:
        if self._fd is not None:
            return None
        if self.try_acquire("probe"):
            self.release("probe")
            return None
        try:
            return self.path.read_text() or "another process"
        except OSError:
            return "another process"


class GeminiSession:
    def __init__(self, settings: WorkerSettings, store: JobStore, lock: ProfileLock,
                 inspector: Callable[[WorkerSettings], dict] | None = None,
                 opener: Callable[[WorkerSettings], subprocess.Popen] | None = None):
        self.settings = settings
        self.store = store
        self.lock = lock
        if inspector is None or opener is None:
            from worker import gemini_browser

            inspector = inspector or gemini_browser.inspect
            opener = opener or gemini_browser.open_plain_window
        self._inspect = inspector
        self._open = opener
        self._proc: subprocess.Popen | None = None
        self._auto: threading.Thread | None = None
        self._reap_lock = threading.Lock()

    @property
    def window_open(self) -> bool:
        self._reap()
        return self._proc is not None

    def _reap(self) -> None:
        with self._reap_lock:
            if self._proc is None or self._proc.poll() is None:
                return
            self._proc = None
            self.lock.release("sign-in")
        log.info("sign-in window closed by the user")
        self.check_in_background()

    def _watch(self, proc: subprocess.Popen) -> None:
        """Notice the user quitting the sign-in window even if nobody is looking at Settings."""
        def run():
            proc.wait()
            self._reap()

        threading.Thread(target=run, name="sign-in-watch", daemon=True).start()

    @property
    def checking(self) -> bool:
        return self._auto is not None and self._auto.is_alive()

    def check_in_background(self) -> None:
        """Check the session without anyone clicking anything (after the sign-in window quits, and at
        worker start so a result left over from before a restart never lingers)."""
        if self.checking:
            return

        def run():
            time.sleep(1.0)  # let Chrome finish writing the profile
            try:
                self.check()
            except Busy as e:
                log.info("automatic session check skipped: %s", e.message)

        self._auto = threading.Thread(target=run, name="session-check", daemon=True)
        self._auto.start()

    def start_check(self) -> dict:
        """Check button: refuse at once if the check can't run, else run it in the background so the
        page can show progress instead of waiting ~20 s on the request."""
        if self.checking:
            return self.status()
        if self.window_open:
            raise Busy("Quit the Gemini sign-in window first (⌘Q in that window).")
        if self.lock.holder is not None or self.lock.held_by_other_process():
            raise Busy("The browser profile is busy (a video is being generated). Check again when it finishes.")
        self.check_in_background()
        return self.status()

    def status(self) -> dict:
        st = dict(self.store.kv_get("gemini_status", {"state": "unknown", "message": "Not checked yet."}))
        pause = self.store.kv_get("account_pause")
        if pause:
            st["state"] = {"limit_reached": "limit_reached",
                           "authentication_required": "signed_out"}.get(pause["kind"], "needs_attention")
            st["message"] = pause["message"]
        st["login_window_open"] = self.window_open
        st["checking"] = self.checking
        return st

    def open_window(self) -> dict:
        if self.window_open:
            return self.status()
        from worker.gemini_browser import PROFILE_IN_USE, profile_browser_pid

        if profile_browser_pid(self.settings) is not None:
            raise Busy(PROFILE_IN_USE.replace("click Check session", "try again"))
        if not self.lock.try_acquire("sign-in"):
            raise Busy("The browser profile is busy (a video is being generated or checked). Try again shortly.")
        try:
            self._proc = self._open(self.settings)
        except Exception as e:
            self.lock.release("sign-in")
            raise Busy(f"Couldn't open the sign-in window ({type(e).__name__}). Run `./run.sh doctor`.") from e
        log.info("sign-in window opened")
        self._watch(self._proc)
        return self.status()

    def done(self, background: bool = False) -> dict:
        """Close the sign-in window for the user (fallback when it was closed with the red ✕, which
        leaves Chrome running on macOS), then check the session — or start that check and return."""
        with self._reap_lock:
            proc, self._proc = self._proc, None
        if proc is not None:
            proc.terminate()  # Chromium shuts down cleanly on SIGTERM and flushes cookies
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            self.lock.release("sign-in")
            if background:
                self.check_in_background()
                return self.status()
            time.sleep(1.0)
        return self.check()

    def check(self) -> dict:
        if self.window_open:
            raise Busy("Quit the Gemini sign-in window first (⌘Q in that window).")
        if not self.lock.try_acquire("session-check"):
            raise Busy("The browser profile is busy (a video is being generated). Check again when it finishes.")
        try:
            result = self._inspect(self.settings)
        except Exception as e:
            log.warning("session check failed: %s", type(e).__name__)
            msg = getattr(e, "user_message", None) or (
                INTERRUPTED if "Connection closed" in str(e)
                else f"Couldn't open the browser ({type(e).__name__}). Run `./run.sh doctor`.")
            result = {"state": "needs_attention", "model": None, "message": msg}
        finally:
            self.lock.release("session-check")
        self.store.kv_set("gemini_status", {"state": result.get("state", "needs_attention"),
                                            "message": result.get("message", ""), "model": result.get("model"),
                                            "plan": result.get("plan"), "video": result.get("video"),
                                            "checked_at": time.time()})
        return result
