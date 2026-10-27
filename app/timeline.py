"""A script from a prompt that already is one: "0–5 seconds: …", "Scene 2 (5–10 sec): …" or "Scene 3: …".

Used when Gemini's planner fails on every attempt: the prompt's own timeline is split into the video's scenes, so a
Gemini chat outage doesn't stop a video whose story is already written out. Gemini's script is still preferred: it adds
the camera and positions. What the prompt itself sets out is kept as Gemini's script would keep it: each person it
describes ("Shinda: a man in his 30s… Voice: …", `bible_from_prompt`), each spoken line ("Shinda says: "…"") as
dialogue rather than words in the action, and its closing "Sound:" note in every scene.
"""
import re

from app.script import TEXT_LIMITS, clean_text

# "Scene 1 (0–5 sec):", "0-5 seconds:", "Scene 2:", "(10–15s):"
MARKER = re.compile(
    r"(?:\bscene\s*(?P<n>\d+)\s*)?\(?\s*(?:(?P<a>\d+(?:\.\d+)?)\s*[–—-]\s*(?P<b>\d+(?:\.\d+)?)\s*"
    r"(?:seconds?|secs?|s)\b)?\s*\)?\s*:", re.I)
# Video-wide notes that follow the timeline: "Visual style: …", "Strict rules: …", "🎵 Music: …"
NOTES = re.compile(r"(?:^|\n|(?<=[.!?\"'”]))\s*(?:[^\w\s\"'“”‘’]{1,3}\s*)?(?:[A-Za-z]+ ){0,3}(?:style|audio|music|rules|"
                   r"animation|language|voice|instructions|format|notes?|sounds?)\s*:", re.I)
SETTING = re.compile(r"\bsetting\s*:\s*(.+?)(?:\n|$)", re.I)
SOUND = re.compile(r"(?:^|\n)\s*(?:[^\w\s]{1,3}\s*)?sounds?\s*:\s*(.+?)(?:\n|$)", re.I)
# "Shinda: A man in his 30s, … Voice: Overconfident…" on a line of its own: a person the prompt describes
PERSON = re.compile(r"(?:^|\n)\s*(?P<name>[^\W\d_][\w'’-]*(?: [^\W\d_][\w'’-]*){0,2})\s*:\s*(?P<text>[^\n]+)")
NOT_A_PERSON = re.compile(r"^(?:setting|sound|sounds|style|visual style|music|background music|audio|title|notes?|"
                          r"rules|strict rules|format|language|camera|scene|location|place|time|mood|tone|genre|"
                          r"animation|ending|hook|story|plot|characters?|dialogue|lines?|voice|voices)$", re.I)
VOICE = re.compile(r"\bvoice\s*:\s*", re.I)
# "Shinda says: "…"", "Billa laughs and asks: “…”": a spoken line (its speaker is the first known name before the verb)
SPEECH_VERBS = (r"says|said|asks|asked|replies|replied|answers|answered|shouts|shouted|yells|yelled|whispers|"
                r"whispered|exclaims|exclaimed|adds|added|cries|cried|calls|called|screams|screamed|mutters|"
                r"muttered|sings|sang|tells \w+|speaks|says to \w+|asks \w+")
QUOTE = r"[\"“”„«»]"


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


def _buckets(segments: list, scene_count: int, clip_seconds: int, timed: bool) -> list[list[str]]:
    """The segments' texts per scene: by their start time, or (untimed) evenly in order."""
    buckets: list[list[str]] = [[] for _ in range(scene_count)]
    for i, (start, _, text) in enumerate(segments):
        at = start if timed else i * scene_count * clip_seconds / len(segments)
        buckets[min(int(at // clip_seconds), scene_count - 1)].append(text)
    return buckets


def bible_from_prompt(prompt: str, cast: list[dict]) -> dict:
    """The story bible a prompt itself sets out, for when Gemini's planner fails: each person it describes on a line
    of their own ("Shinda: a man in his 30s, mustard shalwar kameez… Voice: lively"). A picked character of that
    name gets it as what the prompt says about them (`cast`, which wins over their saved fields); anyone else is a
    person the story brings (`characters`: their look and voice). Only names the story's timeline mentions count."""
    story = " ".join(s[2] for s in _segments(prompt or ""))
    picked = {c["name"].lower() for c in cast}
    people, mine, seen = [], [], set()
    for m in PERSON.finditer(prompt or ""):
        name, text = m.group("name").strip(), m.group("text").strip()
        if (NOT_A_PERSON.match(name) or MARKER.match(m.group(0).strip()) or name.lower() in seen
                or not re.search(rf"(?<!\w){re.escape(name)}(?!\w)", story, re.I)):
            continue
        seen.add(name.lower())
        look, voice = (VOICE.split(text, maxsplit=1) + [""])[:2]
        look, voice = look.strip(" .;,"), voice.strip(" .;,")
        if name.lower() in picked:
            mine.append({"name": name, "look": look, "voice": voice})
        else:
            people.append({"name": name, "look": look, "voice": voice, "never": ""})
    return {"characters": people, "cast": mine}


def _speech(action: str, names: list[str]) -> tuple[str, list[dict]]:
    """The spoken lines written into a moment (`Shinda says: "…"`) as dialogue, and the action without their words,
    so they're spoken in each person's voice and in sync, and never taken for on-screen text."""
    who = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) or r"[^\W\d_][\w'’-]*"
    pattern = re.compile(rf"(?P<lead>(?<!\w)(?P<name>{who})(?!\w)[^.!?:\"“”]{{0,60}}?\b(?:{SPEECH_VERBS}))\s*:?\s*"
                         rf"{QUOTE}(?P<line>[^\"“”„«»]+){QUOTE}\s*[.!?]?", re.I)
    lines = []

    def take(m: re.Match) -> str:
        name = next((n for n in names if n.lower() == m.group("name").lower()), m.group("name"))
        lines.append({"name": name, "line": m.group("line").strip()})
        return m.group("lead") + ". "

    rest = pattern.sub(take, action)
    return re.sub(r" {2,}", " ", re.sub(r"\.\s*\.", ".", rest)).strip(), lines


def script_from_timeline(prompt: str, scene_count: int, cast: list[dict], clip_seconds: int = 10) -> list[dict] | None:
    """`scene_count` raw scenes (as Gemini's planner returns them) from the prompt's own timeline, or None when it
    has none or it doesn't cover every scene. A picked character, or a person the prompt describes
    (`bible_from_prompt`), is in a scene when it's named there; their written lines become the scene's dialogue, and
    the prompt's closing "Sound:" note goes with every scene."""
    segments = _segments(prompt or "")
    if not segments:
        return None
    timed = all(s[0] is not None for s in segments)
    buckets = _buckets(segments, scene_count, clip_seconds, timed)
    if timed and any(not b for b in buckets):
        # The times leave a scene empty: the prompt's timeline is shorter than the scenes (a 20-second prompt made of
        # 8-second Flow scenes is 24 s long), so its moments are spread over the scenes in order instead.
        buckets = _buckets(segments, scene_count, clip_seconds, timed=False)
    if any(not b for b in buckets):
        return None
    m = SETTING.search(prompt)
    setting = clean_text(m.group(1), TEXT_LIMITS["setting"]) if m else ""
    m = SOUND.search(prompt)
    sound = clean_text(m.group(1), TEXT_LIMITS["sound"]) if m else ""
    bible = bible_from_prompt(prompt, cast)
    names = [c["name"] for c in cast] + [c["name"] for c in bible["characters"]]
    scenes = []
    for texts in buckets:
        action = " Then ".join(t.rstrip(" .") + "." for t in texts)
        action, lines = _speech(action, names)
        action = clean_text(action, TEXT_LIMITS["action"], multiline=True)
        said = " ".join([action, *(d["name"] for d in lines)])
        who = [{"name": n} for n in names if re.search(rf"(?<!\w){re.escape(n)}(?!\w)", said, re.I)]
        scenes.append({"setting": setting, "action": action, "characters": who, "dialogue": lines, "sound": sound})
    return scenes
