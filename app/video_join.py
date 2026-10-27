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


MUSIC_VOLUME = 0.35  # the track under the video, before it dips further under speech
LOUDNESS = "loudnorm=I=-16:TP=-1.5:LRA=11"  # every clip's sound at one level, so no clip is louder than the next


def join_clips(paths: list[Path], dest: Path, music: Path | None = None) -> int:
    """Write the clips, in order, to `dest`; return its size. One clip without music is copied as-is (no
    re-encode). `music`: one track laid under the whole video, looped or cut to its length, faded in and out, and
    turned down while anyone speaks. The joined length is checked against the clips'."""
    if not paths:
        raise JoinError("There are no scenes to join.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(paths) == 1 and not music:  # copied, not moved: the scene's clip is kept to watch or remake
        shutil.copyfile(paths[0], dest)
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
            parts.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,{LOUDNESS},aresample=48000[a{i}]")
        else:
            parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={info['duration']:.3f}[a{i}]")
    parts.append("".join(f"[v{i}][a{i}]" for i in range(len(paths))) + f"concat=n={len(paths)}:v=1:a=1[v][a]")
    total = sum(info["duration"] for info in infos)
    out = "[a]"
    if music:
        n, fade = len(paths), min(1.5, total / 4)
        parts += [f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=duration={total:.3f},"
                  f"volume={MUSIC_VOLUME},afade=t=in:d={fade:.2f},afade=t=out:st={total - fade:.3f}:d={fade:.2f}[m]",
                  "[a]asplit=2[speech][key]",
                  "[m][key]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[bed]",
                  "[speech][bed]amix=inputs=2:duration=first:normalize=0[mixed]"]
        out = "[mixed]"
    part = dest.with_suffix(".part")
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y"]
    for p in paths:
        cmd += ["-i", str(p)]
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
    cmd += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", out,
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
    got = _probe(part)["duration"]
    if abs(got - total) > max(0.5, total * 0.02):  # a clip dropped or cut short while joining
        part.unlink(missing_ok=True)
        raise JoinError(f"The joined video came out {got:.1f} s long instead of {total:.1f} s.")
    os.replace(part, dest)
    return dest.stat().st_size


def clip_seconds(clip: Path) -> float:
    try:
        return _probe(clip)["duration"]
    except (JoinError, OSError, subprocess.TimeoutExpired, ValueError):
        return 0.0


def frame_at(clip: Path, dest: Path, seconds: float) -> Path | None:
    """One frame of the clip, `seconds` in, as a JPG at `dest`; None if ffmpeg can't read it."""
    if not shutil.which("ffmpeg") or not clip.exists():
        return None
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{max(seconds, 0):.2f}", "-i",
                            str(clip), "-frames:v", "1", "-q:v", "2", "-update", "1", str(dest)],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return dest if r.returncode == 0 and dest.exists() and dest.stat().st_size else None


def last_frame(clip: Path, dest: Path) -> Path | None:
    """The clip's last frame as a JPG at `dest` (made once), so the next scene can continue from it; None if ffmpeg
    is missing or can't read the clip."""
    if dest.exists():
        return dest
    if not shutil.which("ffmpeg") or not clip.exists():
        return None
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-sseof", "-0.25", "-i", str(clip),
                            "-frames:v", "1", "-q:v", "2", "-update", "1", str(dest)],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return dest if r.returncode == 0 and dest.exists() and dest.stat().st_size else None
