"""The real screen, mouse and keyboard (macOS, Windows or Linux: what differs is in handbot/system/). Buttons are
found by matching small pictures of them (handbot/buttons/<name>.png, taken with `python -m handbot capture`, kept in
git; on Windows and Linux handbot/buttons/<system>/<name>.png first) against a screenshot."""
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pyautogui
import pyperclip

from handbot import system
from handbot.config import BotSettings
from handbot.system import chrome_front, notify, show_app_page  # noqa: F401 — used by flow.py and loop.py
from handbot.system.base import Text

pyautogui.FAILSAFE = True  # push the mouse into a screen corner to stop the bot at once
pyautogui.PAUSE = 0.05
# The button pictures are kept at a Retina screen's 2 pixels per point (`capture` saves them so on any screen), and
# resized to this screen's pixels when matched, so they work on a non-Retina screen too
PICTURE_SCALE = 2.0


def to_scale(img: np.ndarray, k: float) -> np.ndarray:
    """The picture resized by `k` (unchanged when that's 1)."""
    if abs(k - 1) < 0.01:
        return img
    return cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_CUBIC)


@dataclass
class Match:
    x: int  # top-left, in mouse points
    y: int
    w: int
    h: int
    score: float

    def point(self) -> tuple[int, int]:
        """A random spot inside the button, away from its edges."""
        return (self.x + int(self.w * random.uniform(0.3, 0.7)), self.y + int(self.h * random.uniform(0.3, 0.7)))


class Screen:
    def __init__(self, settings: BotSettings):
        self.s = settings
        self._templates: dict[tuple[str, float], np.ndarray] = {}

    # ---- seeing ----------------------------------------------------------
    def grab(self) -> np.ndarray:
        """The main display in grey, at its real pixel size (twice the mouse points on a Retina screen)."""
        return cv2.cvtColor(np.array(system.grab()), cv2.COLOR_RGB2GRAY)

    def scale(self, shot: np.ndarray) -> float:
        return shot.shape[1] / pyautogui.size().width

    def has(self, name: str) -> bool:
        return self.s.button_path(name).exists()

    def _template(self, name: str, k: float) -> np.ndarray:
        """The button's picture at this screen's `k` pixels per point."""
        key = (name, round(k, 2))
        if key not in self._templates:
            path = self.s.button_path(name)
            if not path.exists():
                raise FileNotFoundError(f"No picture of '{name}': run  python -m handbot capture {name}")
            self._templates[key] = to_scale(cv2.imread(str(path), cv2.IMREAD_GRAYSCALE), k / PICTURE_SCALE)
        return self._templates[key]

    def locate(self, name: str, shot: np.ndarray | None = None, area: float = 0.0, either_way: bool = False,
               box: tuple[int, int, int, int] | None = None, outline: bool = False) -> Match | None:
        """Where the button is now, or None. `area`: look only below that fraction of the screen's height.
        `either_way`: also match it with light and dark swapped (a settings option picked or not).
        `box`: look only for a button whose top-left corner lies in (left, top, right, bottom), in mouse points.
        `outline`: match only the button's fine detail (its letters), not the colours behind it (a see-through
        button over a picture that changes)."""
        shot = self.grab() if shot is None else shot
        k = self.scale(shot)
        tpl = self._template(name, k)
        left, top = 0, int(shot.shape[0] * area)
        right, bottom = shot.shape[1], shot.shape[0]
        if box:
            left, top = max(0, int(box[0] * k)), max(top, int(box[1] * k))
            right, bottom = min(right, int(box[2] * k) + tpl.shape[1]), min(bottom, int(box[3] * k) + tpl.shape[0])
        part = shot[top:bottom, left:right]
        if tpl.shape[0] > part.shape[0] or tpl.shape[1] > part.shape[1]:
            return None
        if outline:
            part, tpl = _detail(part), _detail(tpl)
        score, (px, py) = -1.0, (0, 0)
        for t in (tpl, 255 - tpl) if either_way else (tpl,):
            _, sc, _, loc = cv2.minMaxLoc(cv2.matchTemplate(part, t, cv2.TM_CCOEFF_NORMED))
            if sc > score:
                score, (px, py) = sc, loc
        if score < self.s.MATCH_CONFIDENCE:
            return None
        return Match(int((px + left) / k), int((py + top) / k), int(tpl.shape[1] / k), int(tpl.shape[0] / k), score)

    def count(self, name: str, shot: np.ndarray | None = None) -> int:
        """How many times the button shows on the screen (0 if its picture isn't taken)."""
        if not self.has(name):
            return 0
        shot = self.grab() if shot is None else shot
        tpl = self._template(name, self.scale(shot))
        if tpl.shape[0] > shot.shape[0] or tpl.shape[1] > shot.shape[1]:
            return 0
        res = cv2.matchTemplate(shot, tpl, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(res >= self.s.MATCH_CONFIDENCE)
        kept: list[tuple[int, int]] = []  # one per spot: the best match, nearby ones dropped
        for i in np.argsort(-res[ys, xs]):
            x, y = int(xs[i]), int(ys[i])
            if all(abs(x - kx) >= tpl.shape[1] / 2 or abs(y - ky) >= tpl.shape[0] / 2 for kx, ky in kept):
                kept.append((x, y))
        return len(kept)

    def shade(self, m: Match, shot: np.ndarray | None = None) -> float:
        """How light the button's spot on the screen is (0 black – 255 white), mostly its background."""
        shot = self.grab() if shot is None else shot
        k = self.scale(shot)
        return float(np.median(shot[int(m.y * k):int((m.y + m.h) * k), int(m.x * k):int((m.x + m.w) * k)]))

    def texts(self) -> list[Text]:
        """Every line of text on the screen now."""
        path = self.s.BOT_DATA_PATH / "read.png"
        system.screenshot(path)
        try:
            return system.read_text(path, self.size())
        finally:
            path.unlink(missing_ok=True)

    def size(self) -> tuple[int, int]:
        return tuple(pyautogui.size())

    def find(self, name: str, timeout: float = 10, **how) -> Match | None:
        """Wait up to `timeout` seconds for the button to show."""
        end = time.monotonic() + timeout
        while True:
            m = self.locate(name, **how)
            if m or time.monotonic() >= end:
                return m
            time.sleep(0.7)

    def wait_gone(self, name: str, timeout: float = 10) -> bool:
        end = time.monotonic() + timeout
        while self.locate(name):
            if time.monotonic() >= end:
                return False
            time.sleep(0.7)
        return True

    def save(self, path: Path) -> Path:
        system.grab().convert("RGB").save(path, quality=70)
        return path

    # ---- the mouse -------------------------------------------------------
    def move(self, x: int, y: int) -> None:
        """Move the cursor there the way a hand does: a slightly curved path, quick in the middle, slow at the ends."""
        sx, sy = pyautogui.position()
        dist = ((x - sx) ** 2 + (y - sy) ** 2) ** 0.5
        if dist > 40:  # bend the path through a point a little off the straight line
            bend = dist * random.uniform(-0.12, 0.12)
            mx = (sx + x) / 2 - (y - sy) / dist * bend
            my = (sy + y) / 2 + (x - sx) / dist * bend
            pyautogui.moveTo(mx, my, duration=random.uniform(0.2, 0.45), tween=pyautogui.easeInQuad)
        pyautogui.moveTo(x, y, duration=random.uniform(0.2, 0.55), tween=pyautogui.easeOutQuad)

    def move_straight(self, x: int, y: int) -> None:
        """Move in a straight line (into a menu's sub-list, where a curve could leave the menu and close it)."""
        pyautogui.moveTo(x, y, duration=random.uniform(0.25, 0.5), tween=pyautogui.easeInOutQuad)

    def position(self) -> tuple[int, int]:
        return tuple(pyautogui.position())

    def mouse_click(self) -> None:
        pyautogui.click()

    def click(self, name: str, timeout: float = 10, **how) -> Match:
        m = self.find(name, timeout, **how)
        if m is None:
            raise LookupError(f"Couldn't see '{name}' on the screen")
        self.click_at(*m.point())
        return m

    def click_at(self, x: int, y: int) -> None:
        self.move(x, y)
        time.sleep(random.uniform(0.08, 0.25))
        pyautogui.click()

    def hover(self, x: int, y: int) -> None:
        self.move(x, y)

    def scroll(self, clicks: int) -> None:
        """Scroll the page with the mouse over its middle (negative: down)."""
        w, h = pyautogui.size()
        self.move(int(w * random.uniform(0.4, 0.6)), int(h * random.uniform(0.45, 0.6)))
        pyautogui.scroll(clicks)
        time.sleep(random.uniform(0.8, 1.4))

    # ---- the keyboard ----------------------------------------------------
    def keys(self, *keys: str) -> None:
        """A shortcut such as keys(system.MOD, "t"): ⌘T on a Mac, Ctrl+T on Windows and Linux."""
        pyautogui.hotkey(*keys, interval=0.08)
        time.sleep(random.uniform(0.2, 0.4))

    def press(self, key: str) -> None:
        pyautogui.press(key)
        time.sleep(random.uniform(0.15, 0.3))

    def type_slowly(self, text: str) -> None:
        """Type key by key at a person's pace (plain ASCII text only, such as a search or a file path)."""
        for ch in text:
            pyautogui.write(ch)
            time.sleep(random.uniform(0.05, 0.16))

    def paste(self, text: str) -> None:
        """Put the text on the clipboard and press ⌘V / Ctrl+V (keeps long and non-English text exact)."""
        pyperclip.copy(text)
        time.sleep(0.2)
        self.keys(system.MOD, "v")

    # ---- saved points ----------------------------------------------------
    def point(self, name: str) -> tuple[int, int] | None:
        try:
            p = json.loads(self.s.points_path.read_text()).get(name)
        except (OSError, ValueError):
            return None
        return tuple(p) if p else None


def _detail(img: np.ndarray) -> np.ndarray:
    """The image less its blurred self: letters and edges stay, smooth colour behind them goes."""
    return cv2.subtract(img, cv2.GaussianBlur(img, (0, 0), 6))
