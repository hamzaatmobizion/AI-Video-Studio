"""Technical clip checks with ffprobe/ffmpeg."""
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class QCResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration: float = 0.0
    width: int = 0
    height: int = 0


def tools_available() -> bool:
    return bool(shutil.which("ffprobe") and shutil.which("ffmpeg"))


def check_clip(path: Path, aspect: str, min_duration: float) -> QCResult:
    if not path.exists() or path.stat().st_size == 0:
        return QCResult(False, ["The clip file is missing or empty."])
    with open(path, "rb") as f:
        head = f.read(16)
    if not (head[4:8] == b"ftyp" or head.startswith(b"\x1a\x45\xdf\xa3")):
        return QCResult(False, ["The download isn't a real video file."])
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return QCResult(False, ["Couldn't inspect the clip (is ffprobe installed?)."])
    if probe.returncode != 0:
        return QCResult(False, ["The clip couldn't be read."])
    info = json.loads(probe.stdout or "{}")
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        return QCResult(False, ["The clip has no video stream."])
    w, h = int(video.get("width", 0)), int(video.get("height", 0))
    try:
        dur = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    except ValueError:
        dur = 0.0
    res = QCResult(True, duration=dur, width=w, height=h)
    if min(w, h) < 360:
        res.ok = False
        res.reasons.append(f"Resolution too low ({w}x{h}); need at least 360p.")
    if dur < min_duration:
        res.ok = False
        res.reasons.append(f"Clip is only {dur:.1f}s; expected about 8s.")
    portrait = h > w
    if (aspect == "9:16") != portrait:
        res.warnings.append(f"Gemini returned {w}x{h}, which doesn't match {aspect}; it will show letterboxed.")
    try:
        dec = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-f", "null", "-"],
                             capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        res.ok = False
        res.reasons.append("Couldn't decode the clip.")
        return res
    if dec.returncode != 0 or dec.stderr.strip():
        res.ok = False
        res.reasons.append("The clip has decoding errors (corrupt download?).")
    return res
