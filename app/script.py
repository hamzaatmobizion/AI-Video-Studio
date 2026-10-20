"""Structured scripts: what each scene shows, and the clip prompt compiled from it.

Gemini drafts the script as data (who is in the shot and where, the camera, the lines), the user can edit it,
and `compile_prompt` writes the clip prompt. Every library character is described in their own saved words in
every scene, which keeps them looking and sounding the same from clip to clip (Veo never sees the other scenes).

A cast member here is a plain dict: {id, name, appearance, outfit, voice, personality, image_path}.
"""
import re

# Shot vocabulary, offered in the script editor. worker/gemini_browser.py gives Gemini the same lists.
FRAMINGS = ["extreme wide", "wide", "medium", "medium close-up", "close-up", "extreme close-up",
            "over-the-shoulder", "two-shot"]
ANGLES = ["eye level", "low angle", "high angle", "bird's-eye", "dutch angle"]
MOVEMENTS = ["static", "slow push-in", "pull-out", "pan left", "pan right", "tilt up", "tilt down", "tracking",
             "dolly", "handheld", "orbit", "crane"]
LENSES = ["24mm wide-angle lens", "35mm lens", "50mm lens", "85mm lens with shallow depth of field"]
POSITIONS = ["frame left", "center", "frame right", "foreground", "background"]
TIMES_OF_DAY = ["dawn", "morning", "noon", "afternoon", "golden hour", "dusk", "night"]
STYLES = ["Cinematic film, natural light, shallow depth of field", "Pixar-style 3D animation",
          "Anime, hand-drawn look", "Documentary, handheld, natural light", "Glossy TV commercial",
          "Vintage 16mm film, warm grain"]
SHOT_KEYS = ("framing", "angle", "movement", "lens")
SHOT_OPTIONS = {"framing": FRAMINGS, "angle": ANGLES, "movement": MOVEMENTS, "lens": LENSES}

MAX_PROMPT = 2000  # one clip prompt, as Gemini's video tool takes it
MAX_SPOKEN_WORDS = 25  # about what fits in one 10-second clip
MAX_IN_SHOT = 6
MAX_LINES = 8
TEXT_LIMITS = {"setting": 400, "time_of_day": 60, "action": 1500, "sound": 300}
CHARACTER_LIMITS = {"name": 60, "appearance": 400, "outfit": 200, "voice": 150, "personality": 200}


def clean_text(value, limit: int, multiline: bool = False) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"[ \t]+", " ", value) if multiline else re.sub(r"\s+", " ", value)
    return value.strip()[:limit]


def _option(value, options: list[str]) -> str:
    """The canonical option for e.g. "Close-up shot", else the user's own words."""
    text = clean_text(value, 60)
    key = re.sub(r"\s+(shot|angle|lens)$", "", text.lower())
    for opt in options:
        if key in (opt.lower(), re.sub(r"\s+(shot|angle|lens)$", "", opt.lower())):
            return opt
    return text


def _who(item: dict, cast: list[dict]) -> tuple[int | None, str]:
    """(character id, name) for a script reference by id or by name (case-insensitive)."""
    by_id = {c["id"]: c for c in cast}
    cid = item.get("id")
    if isinstance(cid, int) and not isinstance(cid, bool) and cid in by_id:
        return cid, by_id[cid]["name"]
    name = clean_text(item.get("name"), CHARACTER_LIMITS["name"])
    for c in cast:
        if name and c["name"].lower() == name.lower():
            return c["id"], c["name"]
    return None, name


def normalize_scene(raw, cast: list[dict]) -> dict:
    """A scene from Gemini or the editor, as stored: known fields only, bounded, characters matched to the cast.
    A bare string (an old-style prompt) becomes the scene's action."""
    if isinstance(raw, str):
        raw = {"action": raw}
    if not isinstance(raw, dict):
        raw = {}
    scene = {k: clean_text(raw.get(k), n, multiline=k == "action") for k, n in TEXT_LIMITS.items()}
    shot = raw.get("shot") if isinstance(raw.get("shot"), dict) else {}
    scene["shot"] = {k: _option(shot.get(k), SHOT_OPTIONS[k]) for k in SHOT_KEYS}
    people, seen = [], set()
    for item in raw.get("characters") or []:
        item = {"name": item} if isinstance(item, str) else item
        if not isinstance(item, dict):
            continue
        cid, name = _who(item, cast)
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        people.append({"id": cid, "name": name, "position": _option(item.get("position"), POSITIONS)})
    scene["characters"] = people[:MAX_IN_SHOT]
    lines = []
    for item in raw.get("dialogue") or []:
        if not isinstance(item, dict):
            continue
        cid, name = _who(item, cast)
        line = clean_text(item.get("line"), 400)
        if name and line:
            lines.append({"id": cid, "name": name, "line": line})
    scene["dialogue"] = lines[:MAX_LINES]
    return scene


def is_structured(scene: dict) -> bool:
    """True when the scene has more than a bare action (an old-style prompt)."""
    return any(scene.get(k) for k in ("setting", "time_of_day", "sound", "characters", "dialogue")) or \
        any((scene.get("shot") or {}).values())


def _sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text[-1] in ".!?'\"" else text + "."


def compile_prompt(scene: dict, cast: list[dict], style: str = "", language: str = "") -> str:
    """The clip prompt for one scene: style, setting, camera, who is where (in their library words), action,
    lines in each character's voice, sound. A scene that is only an action (and no style) stays as written."""
    if not is_structured(scene) and not style:
        return scene.get("action", "")
    by_id = {c["id"]: c for c in cast}
    parts = []
    if style:
        parts.append(f"Style: {_sentence(style)}")
    setting = ", ".join(x for x in (scene.get("setting"), scene.get("time_of_day")) if x)
    if setting:
        parts.append(f"Setting: {_sentence(setting)}")
    shot = scene.get("shot") or {}
    framing = shot.get("framing", "")
    if framing and "shot" not in framing:
        framing += " shot"
    camera = ", ".join(x for x in (framing, shot.get("angle"), shot.get("movement"), shot.get("lens")) if x)
    if camera:
        parts.append(f"Camera: {_sentence(camera)}")
    people = []
    for p in scene.get("characters") or []:
        sheet = by_id.get(p.get("id")) or {}
        look = "; ".join(x for x in (sheet.get("appearance"), sheet.get("outfit")) if x)
        text = p["name"] + (f" ({look})" if look else "") + (f", {p['position']}" if p.get("position") else "")
        people.append(_sentence(text))
    if people:
        parts.append("Characters: " + " ".join(people))
    if scene.get("action"):
        parts.append(_sentence(scene["action"]))
    lang = f" in {language}" if language else ""
    for d in scene.get("dialogue") or []:
        voice = (by_id.get(d.get("id")) or {}).get("voice")
        who = d["name"] + (f" (voice: {voice})" if voice else "")
        parts.append(f"{who} says{lang}: '{d['line']}'")
    if scene.get("sound"):
        parts.append(f"Sound: {_sentence(scene['sound'])}")
    if scene.get("dialogue"):
        parts.append("Only the named characters speak, clearly and in sync with their lips. No subtitles.")
    return "\n".join(parts)


def spoken_words(scene: dict) -> int:
    return sum(len(d["line"].split()) for d in scene.get("dialogue") or [])


def scene_warnings(scene: dict, prompt: str) -> list[str]:
    out = []
    words = spoken_words(scene)
    if words > MAX_SPOKEN_WORDS:
        out.append(f"{words} spoken words: about {MAX_SPOKEN_WORDS} fit in 10 seconds, so lines may be cut short.")
    if len(prompt) > MAX_PROMPT:
        out.append(f"The clip prompt is {len(prompt):,} characters; shorten this scene to under {MAX_PROMPT:,}.")
    return out


def scene_images(scene: dict, cast: list[dict], fallback: list[str], limit: int = 3) -> list[str]:
    """Reference images for one clip: the pictures of the library characters in the shot, then the
    video's own uploads, at most `limit`."""
    by_id = {c["id"]: c for c in cast}
    paths = [by_id[p["id"]]["image_path"] for p in scene.get("characters") or []
             if p.get("id") in by_id and by_id[p["id"]].get("image_path")]
    out = []
    for p in paths + list(fallback):
        if p not in out:
            out.append(p)
    return out[:limit]


def cast_sheet(c: dict) -> dict:
    """What Gemini is told about a cast member when it drafts the script."""
    return {k: c.get(k) or "" for k in ("name", "appearance", "outfit", "voice", "personality")}
