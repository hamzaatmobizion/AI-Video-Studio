"""Linux (an X11 screen, such as the Xvfb virtual screen of a server): screenshots with mss, text read by Tesseract,
Chrome started and brought to the front with xdotool, Ctrl shortcuts, and the file dialog's Ctrl+L. Linux asks for no
permissions; the screen must be X11 (not Wayland), with a window manager, and DISPLAY set."""
import os
import shutil
import subprocess
import time
from pathlib import Path

from handbot.system import ocr
from handbot.system.base import chrome_problems, dialog_shown, grab_mss, screenshot_mss

NAME = "linux"
MOD = "ctrl"
SWITCH_KEY = "Alt+Tab"
ZOOM_KEY = "Ctrl+0"
FILE_DIALOG = "file dialog"
TAB_CONTROL = False  # the app's page is opened with the keyboard (Ctrl+L or Ctrl+T, its address, Enter)
CHROME_DIR = Path("~/.config/google-chrome").expanduser()
BLACK_SHOT = "The screenshot is black: check the screen (Xvfb) is running and DISPLAY points at it."
CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")

grab = grab_mss
screenshot = screenshot_mss
read_text = ocr.read_text


DIALOG_WORDS = {"open", "location", "recent"}  # besides Cancel, words only the file dialog shows


def dialog_open(lines: list[str]) -> bool:
    """Whether the file dialog is open: Cancel, and Open or another word only it shows."""
    return dialog_shown(lines, DIALOG_WORDS)


def _run(*cmd: str) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def chrome_front() -> None:
    """Start Chrome (your own profile, signed in) if it isn't running, maximized, and bring its window to the front.
    It's never closed."""
    if not _run("pgrep", "-x", "chrome") and not _run("pgrep", "-x", "chromium"):
        exe = next((shutil.which(n) for n in CHROME_NAMES if shutil.which(n)), None)
        if exe is None:
            raise FileNotFoundError("Google Chrome isn't installed.")
        subprocess.Popen([exe, "--start-maximized"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        time.sleep(6)
    _run("xdotool", "search", "--onlyvisible", "--class", "google-chrome|chromium", "windowactivate", "--sync")
    time.sleep(0.8)


def show_app_page(base: str, path: str) -> None:
    raise NotImplementedError("Linux has no control of Chrome's tabs: FlowHand.show_app uses the keyboard")


def notify(title: str, message: str) -> None:
    """A desktop notification, when the screen has a notification service (a server's virtual screen usually hasn't;
    the stop is in the bot's log and on the video's page in your app)."""
    _run("notify-send", title, message[:200])


def keep_awake() -> None:
    """No screen saver or blanking while the bot runs (a virtual screen has none, so this only matters on a desktop)."""
    _run("xset", "s", "off")
    _run("xset", "s", "noblank")
    _run("xset", "-dpms")


def put_file_path(sc, path: Path) -> None:
    """In the open file dialog: Ctrl+L (its location box), the file's path, then Enter (Open)."""
    sc.keys("ctrl", "l")
    time.sleep(0.4)
    sc.paste(str(path.resolve()))
    sc.press("enter")


def problems(s) -> list[str]:
    """What this Linux computer still needs: a screen (DISPLAY), Tesseract, xdotool, a clipboard tool, and Chrome
    saving downloads into DOWNLOAD_DIR without asking, at 100% zoom on Flow."""
    out = []
    if not os.environ.get("DISPLAY"):
        out.append("No screen: start the virtual screen (Xvfb, see handbot/README.md) and set DISPLAY (such as :1).")
    if os.environ.get("WAYLAND_DISPLAY"):
        out.append("This is a Wayland screen: the bot needs X11 (log in with an X11/Xorg session, or use Xvfb).")
    if ocr.tesseract_cmd(s.TESSERACT_CMD) is None:
        out.append("Install Tesseract (sudo apt install tesseract-ocr), or set TESSERACT_CMD in .env.")
    if not shutil.which("xdotool"):
        out.append("Install xdotool (sudo apt install xdotool): it brings Chrome to the front.")
    if not (shutil.which("xclip") or shutil.which("xsel")):
        out.append("Install xclip (sudo apt install xclip): the bot pastes prompts through the clipboard.")
    return out + chrome_problems(s, CHROME_DIR, ZOOM_KEY)
