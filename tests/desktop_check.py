"""The hand bot on a real Windows or Linux desktop, with its own code (run by .github/workflows/handbot-desktop.yml on
GitHub's test machines; not part of pytest). No Google sign-in and no Flow: tests/desktop/page.html, a local page
with text like Flow's and an upload button. Each step is its own command, so each shows as its own step on GitHub:

    python tests/desktop_check.py imports | screenshot | clipboard | page | read | front | match | upload

A screenshot of every step (and of any failure) is saved in $OUT (desktop-out/)."""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import pyautogui  # noqa: E402
import pyperclip  # noqa: E402

from handbot import flow, system  # noqa: E402
from handbot.config import SYSTEM, get_settings  # noqa: E402
from handbot.screen import PICTURE_SCALE, Screen, to_scale  # noqa: E402

OUT = Path(os.environ.get("OUT", "desktop-out")).resolve()
OUT.mkdir(parents=True, exist_ok=True)
PAGE = ROOT / "tests" / "desktop" / "page.html"
S = get_settings()
S.ensure_dirs()
SC = Screen(S)


def shot(tag: str) -> None:
    SC.save(OUT / f"{SYSTEM}-{tag}.jpg")


def fail(message: str, tag: str) -> None:
    shot(f"FAIL-{tag}")
    print(f"✗ {message}")
    sys.exit(1)


def read() -> list:
    texts = SC.texts()
    (OUT / f"{SYSTEM}-last-read.txt").write_text("\n".join(f"{t.x},{t.y} {t.w}x{t.h}  {t.text}" for t in texts),
                                                  encoding="utf-8")
    return texts


def wait_for(test, timeout: float, tag: str, what: str):
    end = time.monotonic() + timeout
    while True:
        found = [t for t in read() if test(t.text)]
        if found:
            return found[0]
        if time.monotonic() > end:
            fail(f"Didn't read {what} on the screen within {timeout:.0f} s", tag)
        time.sleep(1.5)


def step_imports() -> None:
    print(f"system {system.NAME}, shortcut key {system.MOD}, screen {pyautogui.size()} points")
    for p in S.problems() + system.problems(S):
        print(f"  (problem the check reports: {p})")
    print("✓ the bot's code loads")


def step_screenshot() -> None:
    img = np.array(system.grab())
    w, h = pyautogui.size()
    print(f"screenshot {img.shape[1]}×{img.shape[0]} pixels, screen {w}×{h} points")
    if img.shape[1] < w or img.shape[0] < h:
        fail("The screenshot is smaller than the screen", "screenshot")
    path = OUT / f"{SYSTEM}-screenshot.png"
    system.screenshot(path)
    if not path.exists() or cv2.imread(str(path)) is None:
        fail("screenshot(path) saved no picture", "screenshot")
    print("✓ screenshots work")


def step_clipboard() -> None:
    text = "Hand bot: a prompt pasted exactly ✓ 123 — ç"
    pyperclip.copy(text)
    if pyperclip.paste() != text:
        fail(f"The clipboard gave back {pyperclip.paste()!r}", "clipboard")
    print("✓ the clipboard keeps the prompt exact")


def step_page() -> None:
    system.chrome_front()
    time.sleep(4)
    shot("chrome")
    SC.keys(system.MOD, "l")  # the address bar, as the bot does
    SC.paste(PAGE.as_uri())
    SC.press("enter")
    wait_for(lambda t: "desktop test" in t.lower(), 40, "page", "the test page's heading")
    if not np.array(system.grab()).any():
        fail(system.BLACK_SHOT, "page")
    shot("page")
    print("✓ Chrome started, the address was pasted and the page opened")


def step_read() -> None:
    texts = read()
    said = [t.text for t in texts]
    print("\n".join(f"  read: {s}" for s in said))
    checks = {"the prompt box's hint": flow.EMPTY_PROMPT, "\"Failed\"": flow.FAILED, "out of credits": flow.NO_CREDITS}
    missing = [what for what, rx in checks.items() if not any(rx.search(s) for s in said)]
    if missing:
        fail(f"Tesseract didn't read {', '.join(missing)}", "read")
    print("✓ Tesseract reads Flow-like text on a dark page")


def _active_title() -> str:
    if SYSTEM == "windows":
        import pygetwindow as gw

        w = gw.getActiveWindow()
        return w.title if w else ""
    out = subprocess.run(["xdotool", "getactivewindow", "getwindowname"], capture_output=True, text=True)
    return out.stdout.strip()


def step_front() -> None:
    other = subprocess.Popen(["notepad.exe"] if SYSTEM == "windows" else ["xterm"])
    time.sleep(4)
    print(f"in front before: {_active_title()!r}")
    system.chrome_front()
    time.sleep(2)
    title = _active_title()
    print(f"in front after chrome_front: {title!r}")
    other.terminate()
    if "chrome" not in title.lower() and "desktop test" not in title.lower():
        fail(f"Chrome wasn't brought to the front (in front: {title!r})", "front")
    shot("front")
    print("✓ Chrome is brought to the front")


def _upload_button():
    return wait_for(lambda t: "upload media" in t.lower(), 20, "upload", "the UPLOAD MEDIA button")


def step_match() -> None:
    t = _upload_button()
    path = OUT / "match.png"
    system.screenshot(path)
    img = cv2.imread(str(path))
    k = img.shape[1] / pyautogui.size().width
    crop = img[int(t.y * k):int((t.y + t.h) * k), int(t.x * k):int((t.x + t.w) * k)]
    S.capture_dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(S.capture_dir / "desktop_test.png"), to_scale(crop, PICTURE_SCALE / k))  # as `capture` saves it
    m = SC.locate("desktop_test")
    if m is None or abs(m.x - t.x) > 10 or abs(m.y - t.y) > 10:
        fail(f"A button captured on this screen wasn't found again (found {m})", "match")
    print(f"✓ a captured button is found again on this screen (score {m.score:.2f}, at {m.x},{m.y})")


def step_upload() -> None:
    picture = OUT / "3fa2c1-test.png"
    cv2.imwrite(str(picture), np.full((64, 64, 3), 200, np.uint8))
    t = _upload_button()
    SC.click_at(*t.point())
    end = time.monotonic() + 20
    while not system.dialog_open([x.text for x in read()]):
        if time.monotonic() > end:
            fail(f"The {system.FILE_DIALOG} wasn't seen as open (Cancel + {sorted(getattr(system, 'DIALOG_WORDS', []))})",
                 "dialog")
        time.sleep(1)
    shot("dialog")
    print(f"the {system.FILE_DIALOG} is open")
    system.put_file_path(SC, picture)
    wait_for(lambda x: "chosen" in x.lower() and ("3fa2c1" in x.lower().translate(str.maketrans("oil", "011"))
                                                  or "test.png" in x.lower()), 30, "uploaded",
             "\"Chosen: 3fa2c1-test.png\" on the page")
    shot("uploaded")
    print("✓ the picture went in through the real file dialog")


if __name__ == "__main__":
    globals()[f"step_{sys.argv[1]}"]()
