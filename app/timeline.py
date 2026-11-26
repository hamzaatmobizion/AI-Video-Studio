"""A script from a prompt that already is one: "0–5 seconds: …", "Scene 2 (5–10 sec): …" or "Scene 3: …".

Used when Gemini's planner fails on every attempt: the prompt's own timeline is split into the video's scenes, so a
Gemini chat outage doesn't stop a video whose story is already written out. Gemini's script is still preferred: it adds
the camera and positions. What the prompt itself sets out is kept as Gemini's script would keep it: each person it
describes ("Shinda: a man in his 30s… Voice: …", `bible_from_prompt`), each spoken line ("Shinda says: "…"") as
dialogue rather than words in the action, and its closing "Sound:" note in every scene.
"""
import re

from app.script import BIBLE_CAST_LIMITS, MAX_LINES, PRONOUNS, SHOT_KEYS, SPEECH_VERBS, TEXT_LIMITS, clean_text

# "Scene 1 (0–5 sec):", "0-5 seconds:", "Scene 2:", "(10–15s):", "SCENE 2 — 8–16 SECONDS:"
MARKER = re.compile(
    r"(?:\bscene\s*(?P<n>\d+)\s*(?:[—–-]\s*)?)?\(?\s*(?:(?P<a>\d+(?:\.\d+)?)\s*[–—-]\s*(?P<b>\d+(?:\.\d+)?)\s*"
    r"(?:seconds?|secs?|s)\b)?\s*\)?\s*:", re.I)
# Video-wide notes that follow the timeline: "Visual style: …", "Strict rules: …", "🎵 Music: …"
NOTES = re.compile(r"(?:^|\n|(?<=[.!?\"'”]))\s*(?:[^\w\s\"'“”‘’]{1,3}\s*)?(?:[A-Za-z]+ ){0,3}(?:style|audio|music|rules|"
                   r"animation|language|voice|instructions|format|notes?|sounds?|settings|requirements|quality|"
                   r"specifications?|output|export)\s*:", re.I)  # "VIDEO AND AUDIO SETTINGS:" (video 173)
SETTING = re.compile(r"\bsetting\s*:\s*(.+?)(?:\n|$)", re.I)
SOUND = re.compile(r"(?:^|\n)\s*(?:[^\w\s]{1,3}\s*)?sounds?\s*:\s*(.+?)(?:\n|$)", re.I)
# "Shinda: A man in his 30s, … Voice: Overconfident…" on a line of its own: a person the prompt describes
PERSON = re.compile(r"(?:^|\n)\s*(?P<name>[^\W\d_][\w'’-]*(?: [^\W\d_][\w'’-]*){0,2})\s*:\s*(?P<text>[^\n]+)")
NOT_A_PERSON = re.compile(r"^(?:setting|sound|sounds|style|visual style|music|background music|audio|title|notes?|"
                          r"rules|strict rules|format|language|camera|scene|location|place|time|mood|tone|genre|"
                          r"animation|ending|hook|story|plot|characters?|dialogue|lines?|voice|voices)$", re.I)
VOICE = re.compile(r"\bvoice\s*:\s*", re.I)
# "Bahu — Voice: …", "Shinda: a man in his 30s… Voice: …" or a "Voice: …" line under "Shinda: …": whose voice it is
NAMED_LINE = re.compile(r"\s*(?P<name>[^\W\d_][\w'’]*(?:[ -][^\W\d_][\w'’]*){0,2})\s*(?:[—–]|-+|:)\s*(?P<text>.*)")
SPEAKER_VOICE = re.compile(r"(?<![\w-])(?P<name>[^\W\d_][\w'’]*(?: [^\W\d_][\w'’]*){0,2})\s*\(\s*voice\s*:\s*"
                           r"(?P<voice>[^()\n]+)\)", re.I)
# "Camera: medium shot, eye level, slow push-in." written into a moment
CAMERA = re.compile(r"(?<![\w-])camera\s*:", re.I)
# "Shinda says: "…"", "Billa laughs and asks: “…”": a spoken line (its speaker is the first known name before the verb,
# or "She"/"He": the last person named before it)
QUOTE = r"[\"“”„«»]"
ARABIC_SCRIPT = re.compile(r"[\u0600-\u06FF]")  # Urdu and Shahmukhi letters
TITLED = re.compile(rf"\b(?:titled|title|called|named|caption)\b\s*:?\s*{QUOTE}[^\"“”„«»\n]+{QUOTE}", re.I)


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


def _scene_texts(prompt: str, scene_count: int, clip_seconds: int) -> list[list[str]] | None:
    """The prompt's timeline entries for each of the `scene_count` scenes, or None when it has none or they don't
    cover every scene."""
    segments = _segments(prompt or "")
    if not segments:
        return None
    timed = all(s[0] is not None for s in segments)
    buckets = _buckets(segments, scene_count, clip_seconds, timed)
    if timed and any(not b for b in buckets):
        # The times leave a scene empty: the prompt's timeline is shorter than the scenes (a 20-second prompt made of
        # 8-second Flow scenes is 24 s long), so its moments are spread over the scenes in order instead.
        buckets = _buckets(segments, scene_count, clip_seconds, timed=False)
    return None if any(not b for b in buckets) else buckets


# A full brief's timed line: "Beat 1 — Lassi: 0.00 to 1.00 seconds.", "SECTION 6: BEAT 1 — LASSI (0.00–1.00)",
# "TIME: 1.50–3.50 SECONDS", "- 0.20–0.90 seconds: a light ting", "Line 1 (Beat 3, 2.20–3.60 seconds):"
RANGE = re.compile(r"(?P<a>\d+(?:\.\d+)?)\s*(?:[–—-]|to)\s*(?P<b>\d+(?:\.\d+)?)(?!\.?\d|\s*[–—-]\s*\d)"
                   r"\s*(?:seconds?\b|secs?\b|s\b)?", re.I)  # not "1 — 0" in "SCENE 1 — 0–4 SECONDS"
BRIEF_HEAD = re.compile(r"^\W*(?:section|scene|beat|shot|part|time|line|moo|step|master prompt)\b", re.I)


def _timed(line: str) -> re.Match | None:
    """The time range a brief's line starts, when it's a beat's heading or a timed cue, not a range in a sentence
    ("a line needs about 1.3 to 1.6 seconds")."""
    m = RANGE.search(line)
    if not m or len(line) > 200 or float(m.group("a")) >= float(m.group("b")):
        return None
    before = line[:m.start()]
    numbered = re.match(r"^\W*\d+[.)]\s+[A-Z][A-Z &/'—–-]*:?\s*$", before)  # "16. OPENING: 0–2 SECONDS"
    return m if not before.strip(" -•*–—\t") or BRIEF_HEAD.match(line) or "(" in before or numbered else None


def _heading(line: str) -> bool:
    """A brief's section heading ("SECTION 16: MUSIC", "6. FACIAL EXPRESSION AND EMOTIONAL ANIMATION", or a
    section that starts on its label's line: "ANIMATION: Smooth, fluid…"), which ends the timed part above it;
    "CAMERA:" alone on a line or "Action:" inside a beat doesn't."""
    letters = [c for c in line if c.isalpha()]
    caps = len(letters) >= 8 and sum(c.isupper() for c in letters) >= 0.85 * len(letters)
    return (bool(BRIEF_HEAD.match(line)) or (caps and len(line.split()) >= 2 and len(line) <= 80)
            or bool(re.match(r"^\W*[A-Z][A-Z &/'-]{3,}:\s*\S", line)))


def split_brief(prompt: str, clips: int, clip_seconds: int) -> tuple[str, list[str]] | None:
    """A full brief for a video of `clips` clips (the buffalo sarpanch prompt at 16 seconds): its shared sections
    (characters, layout, light, voice, priorities), said in every clip, and each clip's own timed parts (beats,
    timed lines, sound and music cues), by when they start. None unless every clip gets something timed."""
    shared, parts = [], [[] for _ in range(clips)]
    current, head = None, None  # the clip taking lines; a heading right above a timed line ("SCENE 1 — …")
    for line in (prompt or "").splitlines():
        m = _timed(line)
        if m:
            current = parts[min(int(float(m.group("a")) // clip_seconds), clips - 1)]
            if head is not None:  # "SCENE 1 — THE GANGSTER" above "TIME: 0.00–1.50 SECONDS"
                current.append(shared.pop(head))
            head = None
            current.append(line)
        elif _heading(line):
            current = None
            shared.append(line)
            head = len(shared) - 1 if BRIEF_HEAD.match(line) else None  # a scene's title, not "SETTING: …"
        else:
            (shared if current is None else current).append(line)
            if line.strip() and current is None:
                head = None
    if any(not "".join(p).strip() for p in parts):
        return None
    tidy = lambda lines: re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()  # noqa: E731
    return tidy(shared), [tidy(p) for p in parts]


def brief_clips(prompt: str, clips: int, clip_seconds: int) -> list[str] | None:
    """Each clip's prompt from a full brief: the whole brief for one clip; else its shared sections, which part of
    the story this clip is, and the clip's own timed parts (split_brief). None when it doesn't split by time."""
    if clips == 1:
        return [prompt.strip()]
    split = split_brief(prompt, clips, clip_seconds)
    if split is None:
        return None
    shared, parts = split
    return [f"{shared}\n\nTHIS CLIP: part {i} of {clips}, story seconds {(i - 1) * clip_seconds}–{i * clip_seconds} "
            f"(this clip's 0–{clip_seconds}). The sections above describe the whole video; show only the moments "
            f"below, nothing from before or after them.\n\n{part}" for i, part in enumerate(parts, 1)]


# A production script in clips (the AutoFlow scripts): "CHARACTER DESCRIPTION AND CONSISTENCY", then "CLIP 1: …",
# "CLIP 2: …", each with its own scenes, then sections for the editor, not for Flow
NUMBER_WORDS = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"]
CLIP_HEAD = re.compile(rf"^\W*clip\s+(\d+|{'|'.join(NUMBER_WORDS)})\b", re.I)  # "CLIP 2: …", "Clip Two — …"
CAST_HEAD = re.compile(r"^\W*(?:main\s+)?characters?\b", re.I)
EDITOR_HEAD = re.compile(r"^\W*(?:complete\s+(?:ai\s+)?(?:voiceover|dialogue)|(?:final\s+)?video\s+editing\b|"
                         r"final\s+timeline\b|end\s+of\s+(?:production\s+)?script\b)", re.I)
SCREEN_HEAD = re.compile(r"^\W*final\s+screen\s+text\b", re.I)
LOOK_LINE = re.compile(r"^\W*(?:visual\s+)?(?:style|genre|language|music)\s*:", re.I)  # said in every clip


# Without CLIP headings, each SCENE heading is a clip (a story map: "### 🖼 SCENE 1 IMAGE — …", then "### ▶️ SCENE 1 —
# …"; both parts of scene 1 are its clip). A heading line only: never "Scene 1 → 2: …" or a sentence about a scene.
SCENE_HEAD = re.compile(rf"^\s*(?:#{{1,6}}\s*)?[^\w\s]*\s*(?:\*\*)?scene\s+(\d+|{'|'.join(NUMBER_WORDS)})\b"
                        r"(?=\s*(?:$|[:—–\-|`*(]|image\b|video\b|[A-Z]))", re.I)
MD_HEAD = re.compile(r"^(#{1,6})\s")


def _clip_number(line: str) -> int:
    n = (CLIP_HEAD.match(line) or SCENE_HEAD.match(line)).group(1).lower()
    return int(n) if n.isdigit() else NUMBER_WORDS.index(n) + 1


def _clip_heads(lines: list[str]) -> tuple[list[int], int]:
    """The lines that start a clip ("CLIP 1: …", else "SCENE 1 …" when there's no CLIP heading), and the line where
    the editor's sections begin (after the last clip; its own timeline also lists "Clip 1: …")."""
    heads = [i for i, ln in enumerate(lines) if CLIP_HEAD.match(ln)]
    if not heads:
        heads = [i for i, ln in enumerate(lines) if SCENE_HEAD.match(ln) and len(ln) <= 120
                 and not re.match(r"^\s*[-*•]\s", ln)]  # never a bullet ("- **Scene 4:** Goal achieved…")
        if any(MD_HEAD.match(lines[i]) for i in heads):  # a markdown script: only its headings ("### SCENE 1 …"),
            heads = [i for i in heads if MD_HEAD.match(lines[i])]  # not a timeline drawing ("┃ SCENE 2 — …")
    if not heads:
        return [], len(lines)
    editor = next((i for i in range(heads[0], len(lines)) if EDITOR_HEAD.match(lines[i]) or SCREEN_HEAD.match(lines[i])),
                  len(lines))
    return [i for i in heads if i < editor], editor


def by_scenes(prompt: str) -> bool:
    """Whether the script's clips are its SCENE headings (no CLIP heading in it)."""
    return not any(CLIP_HEAD.match(ln) for ln in (prompt or "").splitlines()) and bool(clip_numbers(prompt))


def clip_numbers(prompt: str) -> list[int]:
    """The numbers of the script's clips, in order ([1, 2, 3]): its CLIP headings, else its SCENE headings (a scene
    listed twice, as a story map's picture and then its video, is one clip); [] when it isn't laid out in either."""
    lines = (prompt or "").splitlines()
    return list(dict.fromkeys(_clip_number(lines[i]) for i in _clip_heads(lines)[0]))


def _scene_blocks(lines: list[str], heads: list[int], editor: int) -> dict[int, list[str]]:
    """Each scene's lines, by its number: every part headed with it (a story map's picture, then its video), each
    up to the next scene heading, a markdown heading at its level or above ("## 9. …" after "### SCENE 4 …"), or the
    editor's sections."""
    out: dict[int, list[str]] = {}
    for k, a in enumerate(heads):
        level = MD_HEAD.match(lines[a])
        end = heads[k + 1] if k + 1 < len(heads) else editor
        for i in range(a + 1, end):
            m = MD_HEAD.match(lines[i])
            if level and m and len(m.group(1)) <= len(level.group(1)):
                end = i
                break
        out.setdefault(_clip_number(lines[a]), []).extend([*lines[a:end], ""])
    return out


def layout_clips(prompt: str, clips: int, clip_seconds: int) -> list[str] | None:
    """Each clip's prompt from a production script laid out in clips (CLIP 1, CLIP 2, …), as written: the script's
    style and genre lines and its character section, which clip this is, and that clip's own block (its scenes, image
    and animation prompts, lines and sound). The last clip also gets a closing "FINAL SCREEN TEXT". What's for the
    editor (the complete voiceover script, editing instructions, the final timeline) is left out: Flow would speak
    every line of the video, or burn in subtitles. None unless the script has exactly CLIP 1 to CLIP `clips`."""
    lines = (prompt or "").splitlines()
    heads, editor = _clip_heads(lines)
    if not heads or clip_numbers(prompt) != list(range(1, clips + 1)):
        return None
    tidy = lambda part: re.sub(r"\n{3,}", "\n\n", "\n".join(part)).strip()  # noqa: E731
    before = lines[:heads[0]]
    if by_scenes(prompt):  # its scenes are the clips: all that comes before them (a story map's locked character,
        # look and sound, wherever in it they are) goes with every clip
        look, cast = [], tidy(before)
        parts = _scene_blocks(lines, heads, editor)
        blocks = [tidy(parts[n]) for n in range(1, clips + 1)]
    else:
        cast_at = next((i for i, ln in enumerate(before) if CAST_HEAD.match(ln)), None)
        look = [ln.strip() for ln in before[:cast_at] if LOOK_LINE.match(ln)]
        cast = tidy(before[cast_at:]) if cast_at is not None else ""
        blocks = [tidy(lines[a:b]) for a, b in zip(heads, [*heads[1:], editor])]
    screen = ""
    at = next((i for i in range(editor, len(lines)) if SCREEN_HEAD.match(lines[i])), None)
    if at is not None:  # "FINAL SCREEN TEXT" after the editing instructions (the first layout)
        end = next((i for i in range(at + 1, len(lines)) if EDITOR_HEAD.match(lines[i])), len(lines))
        screen = tidy(lines[at:end])
    if any(not b for b in blocks):
        return None
    out = []
    for n, block in enumerate(blocks, 1):
        note = (f"THIS CLIP: part {n} of {clips}, story seconds {(n - 1) * clip_seconds}–{n * clip_seconds} (this "
                f"clip's 0–{clip_seconds}). The characters above are the whole video's; show only this clip's "
                f"scenes below, nothing from before or after them.") if clips > 1 else ""
        out.append(tidy([*look, "", cast, "", note, "", block, *(["", screen] if n == clips and screen else [])]))
    return out


def _joined(texts: list[str]) -> str:
    return " Then ".join(t.rstrip(" .") + "." for t in texts)


def prompt_voices(prompt: str) -> dict[str, str]:
    """Each voice the prompt gives, by lower-case name: "Bahu — Voice: …", "Shinda: a man… Voice: …", or a
    "Voice: …" line right under "Shinda: …"."""
    voices, last = {}, None
    for line in (prompt or "").splitlines():
        m = NAMED_LINE.match(line)
        if m and not NOT_A_PERSON.match(m.group("name")) and not MARKER.match(line.strip()):
            last, text = m.group("name").strip(), m.group("text")
        elif last and VOICE.match(line.strip()):
            text = line
        else:
            last = None
            continue
        voice = (VOICE.split(text, maxsplit=1) + [""])[1].strip(" .;,")
        if voice:
            voices[last.lower()] = voice
    for m in SPEAKER_VOICE.finditer(prompt or ""):  # "Saas (voice: elderly female, deep) says: "…""
        voices.setdefault(m.group("name").lower(), m.group("voice").strip(" .;,"))
    return voices


def keep_prompt(prompt: str, scenes: list[dict], bible: dict, cast: list[dict],
                clip_seconds: int) -> tuple[list[dict], dict]:
    """Gemini's scenes and bible with what the prompt itself already says kept as written: each moment's action
    (with its "Camera: …", which then replaces Gemini's shot), its spoken lines and who says them, and each voice the
    prompt gives. Gemini's script only fills in the rest (places in the frame, objects, setting, sound). A scene whose
    written lines can't all be matched to a speaker stays as Gemini wrote it."""
    buckets = _scene_texts(prompt, len(scenes), clip_seconds)
    ids = {c["name"].lower(): c["id"] for c in cast}
    names = list(dict.fromkeys([c["name"] for c in cast] + [c["name"] for c in bible.get("characters") or []]))
    out = []
    for sc, texts in zip(scenes, buckets or [None] * len(scenes)):
        action, lines = _speech(_joined(texts), names) if texts else ("", [])
        if not texts or re.search(QUOTE, action):
            out.append(sc)
            continue
        sc = {**sc, "action": clean_text(action, TEXT_LIMITS["action"], multiline=True),
              "dialogue": [{"id": ids.get(d["name"].lower()), **d} for d in lines][:MAX_LINES]}
        if CAMERA.search(action):
            sc["shot"] = dict.fromkeys(SHOT_KEYS, "")
        out.append(sc)
    voices = prompt_voices(prompt)
    bible = {**bible, "characters": [{**c, "voice": voices.get(c["name"].lower()) or c["voice"]}
                                     for c in bible.get("characters") or []]}
    entries = {c["name"].lower(): c for c in bible.get("cast") or []}
    for c in cast:
        voice = voices.get(c["name"].lower())
        if voice:
            entry = entries.setdefault(c["name"].lower(), {**dict.fromkeys(BIBLE_CAST_LIMITS, ""), "name": c["name"]})
            entries[c["name"].lower()] = {**entry, "voice": voice}
    bible["cast"] = list(entries.values())
    return out, bible


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


def missing_lines(prompt: str, scenes: list[dict]) -> list[str]:
    """Each line the prompt writes in quotes (of two words or more) that no scene says word for word: the script
    dropped or reworded it. Punctuation, spacing and capitals don't count. Lines written in Roman letters that the
    script says in Urdu script (Shahmukhi), as Pakistani Punjabi is spoken, count as said when it has as many of
    them (video 168: "Oye! Main Punjab da sab ton smart banda aan!" → "اوئے! میں پنجاب دا…")."""
    lines = [d["line"] for sc in scenes for d in sc.get("dialogue") or []]
    said = re.sub(r"[\W_]+", "", " ".join(lines)).casefold()
    out = []
    # a title in quotes ('titled: "پپو دا لیٹ ویاہ 😂"', video 172) isn't a line anyone says
    spoken = TITLED.sub(" ", prompt or "")
    for line in re.findall(rf"{QUOTE}([^\"“”„«»\n]+){QUOTE}", spoken):
        words = re.sub(r"[\W_]+", "", line).casefold()
        if len(line.split()) > 1 and words and words not in said and line.strip() not in out:
            out.append(line.strip())
    roman = [x for x in out if not ARABIC_SCRIPT.search(x)]
    if roman and sum(bool(ARABIC_SCRIPT.search(x)) for x in lines) >= len(roman):  # written out in Shahmukhi
        out = [x for x in out if x not in roman]
    return out


def _speech(action: str, names: list[str]) -> tuple[str, list[dict]]:
    """The spoken lines written into a moment (`Shinda says: "…"`) as dialogue, and the action without their words,
    so they're spoken in each person's voice and in sync, and never taken for on-screen text."""
    known = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    who = "|".join([known, *PRONOUNS]) if known else r"[^\W\d_][\w'’-]*"
    named = re.compile(rf"(?<!\w)(?:{known})(?!\w)", re.I) if known else None
    pattern = re.compile(rf"(?P<lead>(?<!\w)(?P<name>{who})(?!\w)(?P<paren>\s*\([^()\n]*\))?[^.!?:\"“”]{{0,300}}?"
                         rf"\b(?:{SPEECH_VERBS})"
                         rf"(?:\s+[^\W\d_]+ly)?)\s*:?\s*"
                         rf"{QUOTE}(?P<line>[^\"“”„«»]+){QUOTE}\s*[.!?]?", re.I)
    lines = []

    def take(m: re.Match) -> str:
        said = m.group("name")
        if known and said.lower() in PRONOUNS:  # "She turns to Saas and says": whom the sentence before is about
            sentences = [s for s in re.split(r"(?<=[.!?])\s+", action[:m.start()]) if named.search(s)]
            if not sentences:
                return m.group(0)
            said = named.search(sentences[-1]).group(0)
        name = next((n for n in names if n.lower() == said.lower()), said)
        lines.append({"name": name, "line": m.group("line").strip()})
        lead = m.group("lead")  # "Saas (voice: …) says" → "Saas says": the voice is taken by prompt_voices
        return (lead.replace(m.group("paren"), "", 1) if m.group("paren") else lead) + ". "

    rest = pattern.sub(take, action)
    return re.sub(r" {2,}", " ", re.sub(r"\.\s*\.", ".", rest)).strip(), lines


def script_from_timeline(prompt: str, scene_count: int, cast: list[dict], clip_seconds: int = 10) -> list[dict] | None:
    """`scene_count` raw scenes (as Gemini's planner returns them) from the prompt's own timeline, or None when it
    has none or it doesn't cover every scene. A picked character, or a person the prompt describes
    (`bible_from_prompt`), is in a scene when it's named there; their written lines become the scene's dialogue, and
    the prompt's closing "Sound:" note goes with every scene."""
    buckets = _scene_texts(prompt, scene_count, clip_seconds)
    if buckets is None:
        return None
    m = SETTING.search(prompt)
    setting = clean_text(m.group(1), TEXT_LIMITS["setting"]) if m else ""
    m = SOUND.search(prompt)
    sound = clean_text(m.group(1), TEXT_LIMITS["sound"]) if m else ""
    bible = bible_from_prompt(prompt, cast)
    names = [c["name"] for c in cast] + [c["name"] for c in bible["characters"]]
    scenes = []
    for texts in buckets:
        action, lines = _speech(_joined(texts), names)
        action = clean_text(action, TEXT_LIMITS["action"], multiline=True)
        said = " ".join([action, *(d["name"] for d in lines)])
        who = [{"name": n} for n in names if re.search(rf"(?<!\w){re.escape(n)}(?!\w)", said, re.I)]
        scenes.append({"setting": setting, "action": action, "characters": who, "dialogue": lines, "sound": sound})
    return scenes
