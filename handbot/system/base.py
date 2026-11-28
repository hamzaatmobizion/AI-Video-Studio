"""What every system's part shares: a line of text read off the screen, screenshots for Windows and Linux, and the
check of Chrome's own settings."""
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Text:
    """A line of text read off the screen, and where it is (mouse points)."""
    text: str
    x: int
    y: int
    w: int
    h: int

    def point(self) -> tuple[int, int]:
        return (self.x + int(self.w * random.uniform(0.3, 0.7)), self.y + int(self.h * random.uniform(0.35, 0.65)))


def dialog_shown(lines: list[str], others: set[str]) -> bool:
    """Windows and Linux: whether the file dialog is open, from the lines read off the screen. "Cancel" and one word
    only the dialog shows (Tesseract can miss "Open" on its coloured button), each a word within any line."""
    words = [" ".join(re.findall(r"[a-z]+", line.lower())) for line in lines]
    has = lambda w: any(re.search(rf"\b{w}\b", line) for line in words)  # noqa: E731
    return has("cancel") and any(has(w) for w in others)


def grab_mss():
    """The main display (Windows, Linux) as an RGB picture, at its real pixel size."""
    import mss
    from PIL import Image

    with mss.mss() as m:
        shot = m.grab(m.monitors[1])
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def screenshot_mss(path: Path) -> None:
    grab_mss().save(path)


def chrome_problems(s, chrome_dir: Path, zoom_key: str) -> list[str]:
    """Chrome saving downloads into DOWNLOAD_DIR without asking, at 100% zoom on Flow (read from its settings files)."""
    try:
        profile = json.loads((chrome_dir / "Local State").read_text()).get("profile", {}).get("last_used") or "Default"
        prefs = json.loads((chrome_dir / profile / "Preferences").read_text())
    except (OSError, ValueError):
        return ["Open Chrome once and sign in to Google and to Flow (no Chrome settings found)."]
    out = []
    download = prefs.get("download", {})
    if download.get("prompt_for_download"):
        out.append("In Chrome, Settings → Downloads: turn off \"Ask where to save each file\".")
    folder = Path(download.get("default_directory") or "~/Downloads").expanduser()
    if folder.resolve() != s.DOWNLOAD_DIR.resolve():
        out.append(f"Chrome saves downloads in {folder}, not {s.DOWNLOAD_DIR}: change one of them "
                   "(DOWNLOAD_DIR in .env).")
    zoom = prefs.get("partition", {})
    default = zoom.get("default_zoom_level", {}).get("x", 0)
    flow = zoom.get("per_host_zoom_levels", {}).get("x", {}).get("flow.google.com", {})
    if abs(flow.get("zoom_level", default)) > 0.01:
        out.append(f"Chrome's zoom on Flow isn't 100%: press {zoom_key} on Flow's page.")
    return out
