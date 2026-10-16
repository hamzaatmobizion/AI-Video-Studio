"""Join scene clips into one MP4 with ffmpeg (hard cuts)."""
import json
import os
import shutil
import subprocess
from pathlib import Path

JOIN_TIMEOUT = 900  # seconds; 12 × 10 s at 720p re-encodes in well under this


class JoinError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _probe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise JoinError(f"{path.name} isn't a readable video.")
    info = json.loads(r.stdout or "{}")
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise JoinError(f"{path.name} has no video stream.")
    return {"width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
            "audio": any(s.get("codec_type") == "audio" for s in streams),
            "duration": float(info.get("format", {}).get("duration") or video.get("duration") or 0)}


def join_clips(paths: list[Path], dest: Path) -> int:
    """Write the clips, in order, to `dest`; return its size. One clip is moved as-is (no re-encode)."""
    if not paths:
        raise JoinError("There are no scenes to join.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(paths) == 1:
        os.replace(paths[0], dest)
        return dest.stat().st_size
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        raise JoinError("ffmpeg isn't installed, so the scenes can't be joined.")
    infos = [_probe(p) for p in paths]
    w, h = infos[0]["width"], infos[0]["height"]
    # Re-encode through the concat filter: every input is fitted to the first clip's size and frame rate,
    # and a clip without sound gets silence, so clips that differ slightly still join cleanly.
    parts = []
    for i, info in enumerate(infos):
        parts.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,format=yuv420p[v{i}]")
        if info["audio"]:
            parts.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
        else:
            parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={info['duration']:.3f}[a{i}]")
    parts.append("".join(f"[v{i}][a{i}]" for i in range(len(paths))) + f"concat=n={len(paths)}:v=1:a=1[v][a]")
    part = dest.with_suffix(".part")
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y"]
    for p in paths:
        cmd += ["-i", str(p)]
    cmd += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", "-f", "mp4", str(part)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=JOIN_TIMEOUT)
    except subprocess.TimeoutExpired:
        part.unlink(missing_ok=True)
        raise JoinError("Joining the scenes took too long.") from None
    if r.returncode != 0 or not part.exists():
        part.unlink(missing_ok=True)
        last = (r.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
        raise JoinError(f"ffmpeg couldn't join the scenes ({last[0][:200]}).")
    os.replace(part, dest)
    return dest.stat().st_size
