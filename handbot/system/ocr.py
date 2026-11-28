"""Reading the screen's words with Tesseract (Windows and Linux; macOS uses its own Vision).

Tesseract reads dark text on a light background best, and Flow is dark, so each screenshot is read as it is and with
light and dark swapped, and a word found both ways is kept once. Then it's read once more evened out (each pixel
against the colour around it, so any text is dark on light whatever its background): that finds a light button's dark
words on a dark page, which the other two miss, and only adds lines where they found nothing (it misreads some
letters they get right, such as "|hat" for "What"). Small screen text reads better enlarged, so the
screenshot is read at OCR_SCALE pixels per point. Tesseract gives single words; words on one line that sit close
together are joined into one line of text, as macOS's Vision gives them (a button's words stay one line, two buttons
side by side stay apart)."""
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from handbot.system.base import Text

LINE_GAP = 0.7  # words this close (× their height) on one line are one line of text
SAME_LINE = 0.4  # word centres at most this far apart (× their height) are on one line
BACKGROUND = 25  # the evened-out reading: the colour around a pixel is taken this many points across
WINDOWS_TESSERACT = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe"


@dataclass
class Word:
    text: str
    x: int
    y: int
    w: int
    h: int
    conf: float


def tesseract_cmd(configured: str = "") -> str | None:
    """The tesseract program: TESSERACT_CMD, else on PATH, else where its Windows installer puts it."""
    if configured:
        return configured
    found = shutil.which("tesseract")
    if found:
        return found
    return str(WINDOWS_TESSERACT) if WINDOWS_TESSERACT.exists() else None


def read_text(image: Path, size: tuple[int, int]) -> list[Text]:
    """The lines of text in a screenshot, placed on a screen `size` points wide and high."""
    import cv2
    import pytesseract

    from handbot.config import get_settings

    s = get_settings()
    cmd = tesseract_cmd(s.TESSERACT_CMD)
    if cmd is None:
        raise FileNotFoundError("Tesseract isn't installed (or set TESSERACT_CMD in .env): the bot can't read the "
                                "screen's words without it.")
    pytesseract.pytesseract.tesseract_cmd = cmd
    grey = cv2.imread(str(image), cv2.IMREAD_GRAYSCALE)
    if grey is None:
        return []
    per_point = grey.shape[1] / size[0]
    up = max(1.0, s.OCR_SCALE / per_point)
    if up > 1.01:
        grey = cv2.resize(grey, None, fx=up, fy=up, interpolation=cv2.INTER_CUBIC)
    k = int(BACKGROUND * up * per_point) | 1  # odd, as the median blur needs
    evened = 255 - cv2.absdiff(grey, cv2.medianBlur(grey, k))

    def words_in(*images) -> list[Word]:
        out = []
        for img in images:
            d = pytesseract.image_to_data(img, config="--psm 11", output_type=pytesseract.Output.DICT)
            for i, text in enumerate(d["text"]):
                conf = float(d["conf"][i])
                if text.strip() and conf >= s.OCR_MIN_CONFIDENCE:
                    out.append(Word(text.strip(), d["left"][i], d["top"][i], d["width"][i], d["height"][i], conf))
        return out

    scale = up * per_point
    return fill_in(lines(dedupe(words_in(grey, 255 - grey)), scale), lines(dedupe(words_in(evened)), scale))


def _overlap(a: Word, b: Word) -> float:
    """How much two word boxes overlap, 0–1, of the smaller one."""
    w = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    h = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    if w <= 0 or h <= 0:
        return 0.0
    return w * h / max(1, min(a.w * a.h, b.w * b.h))


def dedupe(words: list[Word]) -> list[Word]:
    """One word per spot: a word read both ways (or twice) is kept once, the surer reading."""
    kept: list[Word] = []
    for w in sorted(words, key=lambda w: -w.conf):
        if all(_overlap(w, k) < 0.5 for k in kept):
            kept.append(w)
    return kept


def _covers(a: Text, b: Text) -> bool:
    """Whether two lines sit in the same spot (most of the smaller one inside the other)."""
    w = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    h = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    return w > 0 and h > 0 and w * h >= 0.3 * max(1, min(a.w * a.h, b.w * b.h))


def fill_in(first: list[Text], more: list[Text]) -> list[Text]:
    """`first`'s lines, and those of `more` that sit where `first` found nothing and have words (3 letters or more:
    icons read evened out come out as short junk, such as "ok")."""
    out = first + [m for m in more if len(re.sub(r"[^A-Za-z]", "", m.text)) >= 3
                   and not any(_covers(m, f) for f in first)]
    return sorted(out, key=lambda t: (t.y, t.x))


def lines(words: list[Word], k: float) -> list[Text]:
    """Words joined into lines of text (left to right, close together, on one line), in mouse points (`k` pixels
    per point)."""
    rows: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.x, w.y)):
        for row in rows:
            last = row[-1]
            h = max(last.h, w.h)
            gap = w.x - (last.x + last.w)
            if abs((last.y + last.h / 2) - (w.y + w.h / 2)) <= SAME_LINE * h and -0.3 * h <= gap <= LINE_GAP * h:
                row.append(w)
                break
        else:
            rows.append([w])
    out = []
    for row in rows:
        x0, y0 = min(w.x for w in row), min(w.y for w in row)
        x1, y1 = max(w.x + w.w for w in row), max(w.y + w.h for w in row)
        out.append(Text(" ".join(w.text for w in row), int(x0 / k), int(y0 / k), int((x1 - x0) / k),
                        int((y1 - y0) / k)))
    return sorted(out, key=lambda t: (t.y, t.x))
