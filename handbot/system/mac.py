"""macOS: screenshots with screencapture, text read by macOS's own Vision, Chrome brought to the front and its tabs
switched with AppleScript, the Mac file dialog's ⌘⇧G, and the terminal's Accessibility and Screen Recording."""
import ctypes
import os
import random
import subprocess
import time
from pathlib import Path

import pyautogui

from handbot.system.base import Text, chrome_problems

NAME = "mac"
MOD = "command"  # the shortcut key: ⌘V, ⌘L, ⌘T
SWITCH_KEY = "⌘Tab"
ZOOM_KEY = "⌘0"
FILE_DIALOG = "Mac file dialog"
TAB_CONTROL = True  # the app's tab is found and loaded through macOS's control of Chrome (show_app_page)
CHROME_DIR = Path("~/Library/Application Support/Google/Chrome").expanduser()
BLACK_SHOT = ("The screenshot is black: allow Screen Recording for this terminal (System Settings → Privacy & "
              "Security), then restart it.")


def grab():
    return pyautogui.screenshot()


def screenshot(path: Path) -> None:
    subprocess.run(["screencapture", "-x", str(path)], check=True)


def read_text(image: Path, size: tuple[int, int]) -> list[Text]:
    """The lines of text in a screenshot (macOS's own text recognition, on this Mac), placed on a screen `size`
    points wide and high."""
    import Vision
    from Foundation import NSURL

    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(False)
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(image)), None)
    ok, _ = handler.performRequests_error_([req], None)
    out = []
    for obs in (req.results() or []) if ok else []:
        cand = obs.topCandidates_(1)
        if not cand:
            continue
        (bx, by), (bw, bh) = obs.boundingBox()  # 0–1, from the bottom left
        out.append(Text(str(cand[0].string()), int(bx * size[0]), int((1 - by - bh) * size[1]),
                        int(bw * size[0]), int(bh * size[1])))
    return out


def dialog_open(lines: list[str]) -> bool:
    """Whether the Mac file dialog is open: its Cancel and Open buttons, each read as a line of its own."""
    return {"cancel", "open"} <= {line.strip().lower() for line in lines}


def chrome_front() -> None:
    """Start Chrome (your own profile, signed in) if it isn't running, and bring it to the front. It's never quit."""
    if subprocess.run(["pgrep", "-x", "Google Chrome"], capture_output=True).returncode != 0:
        subprocess.run(["open", "-a", "Google Chrome"], check=True)
        time.sleep(6)
    subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to activate'], check=True)
    time.sleep(0.8)


SHOW_PAGE = """
on run argv
    set target to item 1 of argv
    set base to item 2 of argv
    tell application "Google Chrome"
        activate
        repeat with w in windows
            set i to 0
            repeat with t in tabs of w
                set i to i + 1
                if (URL of t) starts with base then
                    set active tab index of w to i
                    set index of w to 1
                    set URL of t to target
                    return "found"
                end if
            end repeat
        end repeat
        tell window 1 to make new tab with properties {URL:target}
        return "new"
    end tell
end run
"""


def show_app_page(base: str, path: str) -> None:
    """Bring your app's tab in Chrome to the front (any tab already on the app, else a new one) and load `path` there.
    macOS's own control of Chrome's tabs (the app's tab titles keep changing, so its tab can't be found by sight);
    it never touches Flow's tabs."""
    subprocess.run(["osascript", "-e", SHOW_PAGE, base + path, base], check=True, capture_output=True, timeout=60)


def notify(title: str, message: str) -> None:
    """A Mac notification with a sound, so you notice the bot needs you."""
    script = "on run argv\n display notification (item 2 of argv) with title (item 1 of argv) sound name \"Glass\"\nend run"
    subprocess.run(["osascript", "-e", script, title, message[:200]], capture_output=True, timeout=10)


def keep_awake() -> None:
    """Keep the screen awake and unlocked while the bot runs: it can't see or click a sleeping screen."""
    subprocess.Popen(["caffeinate", "-di", "-w", str(os.getpid())])


def put_file_path(sc, path: Path) -> None:
    """In the open Mac file dialog: ⌘⇧G, the file's path, then Open."""
    sc.keys(MOD, "shift", "g")
    time.sleep(0.6)
    # pasted, not typed: key by key, the temp folder's "__" opened the Emoji viewer, which took the rest
    sc.paste(str(path.resolve()))
    sc.press("enter")
    time.sleep(random.uniform(0.8, 1.4))
    sc.press("enter")  # Open


def problems(s) -> list[str]:
    """What this Mac still needs: the terminal's Accessibility and Screen Recording permissions, and Chrome saving
    downloads into DOWNLOAD_DIR without asking, at 100% zoom on Flow."""
    out = []
    ax = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    ax.AXIsProcessTrusted.restype = ctypes.c_bool
    if not ax.AXIsProcessTrusted():
        out.append("Allow Accessibility for this terminal (System Settings → Privacy & Security), then restart it.")
    cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
    if not cg.CGPreflightScreenCaptureAccess():
        out.append("Allow Screen Recording for this terminal (System Settings → Privacy & Security), then restart it.")
    return out + chrome_problems(s, CHROME_DIR, ZOOM_KEY)
