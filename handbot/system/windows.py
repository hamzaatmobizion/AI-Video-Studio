"""Windows: screenshots with mss, text read by Tesseract, Chrome started and brought to the front as a window, Ctrl
shortcuts, and the Windows file dialog's File name box. Windows asks for no permissions; the desktop only has to stay
open (signed in, not locked, not hidden by a closed Remote Desktop: see handbot/README.md)."""
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import pyautogui

from handbot.system import ocr
from handbot.system.base import chrome_problems, dialog_shown, grab_mss, screenshot_mss

NAME = "windows"
MOD = "ctrl"
SWITCH_KEY = "Alt+Tab"
ZOOM_KEY = "Ctrl+0"
FILE_DIALOG = "Windows file dialog"
TAB_CONTROL = False  # the app's page is opened with the keyboard (Ctrl+L or Ctrl+T, its address, Enter)
CHROME_DIR = Path(os.environ.get("LOCALAPPDATA") or "~/AppData/Local").expanduser() / "Google" / "Chrome" / "User Data"
BLACK_SHOT = ("The screenshot is black: keep the Windows desktop open (signed in and not locked; after closing Remote "
              "Desktop, use the tscon command in handbot/README.md), then try again.")
CHROME_EXE = [Path(os.environ.get(v) or "") / "Google" / "Chrome" / "Application" / "chrome.exe"
              for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]

grab = grab_mss
screenshot = screenshot_mss
read_text = ocr.read_text
log = logging.getLogger("handbot")


DIALOG_WORDS = {"open", "file name"}  # besides Cancel, words only the file dialog shows


def dialog_open(lines: list[str]) -> bool:
    """Whether the file dialog is open: Cancel, and Open or another word only it shows."""
    return dialog_shown(lines, DIALOG_WORDS)


def _chrome_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"], capture_output=True, text=True)
    return "chrome.exe" in out.stdout.lower()


def chrome_front() -> None:
    """Start Chrome (your own profile, signed in) if it isn't running, bring its window to the front and keep it
    maximized. It's never closed."""
    import pygetwindow as gw

    if not _chrome_running():
        exe = next((str(p) for p in CHROME_EXE if p.is_file()), None) or shutil.which("chrome")
        if exe is None:
            raise FileNotFoundError("Google Chrome isn't installed.")
        subprocess.Popen([exe, "--start-maximized"], creationflags=subprocess.DETACHED_PROCESS,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(6)
    wins = [w for w in gw.getWindowsWithTitle("Google Chrome") if w.title.endswith("Google Chrome")]
    if not wins:
        log.warning("no Chrome window found to bring to the front")
        return
    w = wins[0]
    if w.isMinimized:
        w.restore()
    pyautogui.press("alt")  # Windows lets a program bring a window to the front just after a key press
    try:
        w.activate()
    except Exception:  # noqa: BLE001 — pygetwindow can report an error even when the window came to the front
        pass
    if not w.isMaximized:
        w.maximize()
    time.sleep(0.8)


def show_app_page(base: str, path: str) -> None:
    raise NotImplementedError("Windows has no control of Chrome's tabs: FlowHand.show_app uses the keyboard")


def notify(title: str, message: str) -> None:
    """Nothing to show on a server: the stop is in the bot's log and on the video's page in your app."""


def keep_awake() -> None:
    """Keep the display and the computer awake while the bot runs (for as long as this program runs)."""
    import ctypes

    es_continuous, es_system, es_display = 0x80000000, 0x00000001, 0x00000002
    ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system | es_display)


def put_file_path(sc, path: Path) -> None:
    """In the open Windows file dialog: Alt+N (its File name box), the file's path, then Enter (Open)."""
    sc.keys("alt", "n")
    time.sleep(0.4)
    sc.paste(str(path.resolve()))
    sc.press("enter")


def problems(s) -> list[str]:
    """What this Windows computer still needs: Tesseract, and Chrome saving downloads into DOWNLOAD_DIR without
    asking, at 100% zoom on Flow."""
    out = []
    if ocr.tesseract_cmd(s.TESSERACT_CMD) is None:
        out.append("Install Tesseract (winget install UB-Mannheim.TesseractOCR), or set TESSERACT_CMD in .env.")
    return out + chrome_problems(s, CHROME_DIR, ZOOM_KEY)
