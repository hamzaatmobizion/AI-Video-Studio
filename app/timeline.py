"""A script from a prompt that already is one: "0–5 seconds: …", "Scene 2 (5–10 sec): …" or "Scene 3: …".

Used when Gemini's planner fails on every attempt: the prompt's own timeline is split into the 10-second scenes, so a
Gemini chat outage doesn't stop a video whose story is already written out. Gemini's script is still preferred: it adds
the camera, positions and the story bible.
"""
import re

from app.script import TEXT_LIMITS, clean_text

# "Scene 1 (0–5 sec):", "0-5 seconds:", "Scene 2:", "(10–15s):"
MARKER = re.compile(
    r"(?:\bscene\s*(?P<n>\d+)\s*)?\(?\s*(?:(?P<a>\d+(?:\.\d+)?)\s*[–—-]\s*(?P<b>\d+(?:\.\d+)?)\s*"
    r"(?:seconds?|secs?|s)\b)?\s*\)?\s*:", re.I)
# Video-wide notes that follow the timeline: "Visual style: …", "Strict rules: …", "🎵 Music: …"
NOTES = re.compile(r"(?:^|\n|(?<=[.!?\"'”]))\s*(?:[^\w\s]{1,3}\s*)?(?:[A-Za-z]+ ){0,3}(?:style|audio|music|rules|"
                   r"animation|language|voice|instructions|format|notes?)\s*:", re.I)
SETTING = re.compile(r"\bsetting\s*:\s*(.+?)(?:\n|$)", re.I)


def _segments(prompt: str) -> list[tuple[float | None, int | None, str]]:
    """(start second, scene number, text) for each timeline entry, in order."""
    marks = [m for m in MARKER.finditer(prompt) if m.group("n") or m.group("a")]
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(prompt)
        text = prompt[m.end():end]
        if i + 1 == len(marks):  # the last entry stops where the video-wide notes begin
            note = NOTES.search(text)
            text = text[:note.start()] if note else text
        text = re.sub(r"[^\S\n]*[^\w\s\"'“”‘’()]*\s*(?:background |ending )?music\s*:.*", "", text, flags=re.I)
        start = float(m.group("a")) if m.group("a") else None
        out.append((start, int(m.group("n")) if m.group("n") else None, clean_text(text, 2000)))
    return [s for s in out if s[2]]


def story_moments(prompt: str) -> int:
    """How many timed or numbered moments the prompt sets out ("0–5 seconds: …", "Scene 2: …")."""
    return len(_segments(prompt or ""))


def script_from_timeline(prompt: str, scene_count: int, cast: list[dict], clip_seconds: int = 10) -> list[dict] | None:
    """`scene_count` raw scenes (as Gemini's planner returns them) from the prompt's own timeline, or None when it
    has none or it doesn't cover every scene. A picked character is in a scene when it's named there."""
    segments = _segments(prompt or "")
    if not segments:
        return None
    timed = all(s[0] is not None for s in segments)
    buckets: list[list[str]] = [[] for _ in range(scene_count)]
    for i, (start, _, text) in enumerate(segments):
        at = start if timed else i * scene_count * clip_seconds / len(segments)
        buckets[min(int(at // clip_seconds), scene_count - 1)].append(text)
    if any(not b for b in buckets):
        return None
    m = SETTING.search(prompt)
    setting = clean_text(m.group(1), TEXT_LIMITS["setting"]) if m else ""
    scenes = []
    for texts in buckets:
        action = clean_text(" Then ".join(t.rstrip(" .") + "." for t in texts), TEXT_LIMITS["action"], multiline=True)
        who = [{"name": c["name"]} for c in cast if re.search(rf"\b{re.escape(c['name'])}\b", action, re.I)]
        scenes.append({"setting": setting, "action": action, "characters": who})
    return scenes
