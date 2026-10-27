"""Structured scripts: what each scene shows, and the clip prompt compiled from it.

Gemini drafts the script as data (who is in the shot and where, the camera, the lines), the user can edit it,
and `compile_prompt` writes the clip prompt. Every library character is described in their own saved words in
every scene, which keeps them looking and sounding the same from clip to clip (Veo never sees the other scenes).

A cast member here is a plain dict: {id, name, appearance, outfit, voice, personality, image_path}.
"""
import re

from app.genres import clip_avoid

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
MAX_SPOKEN_WORDS = 25  # about what fits in one 10-second clip (words_per_clip for other lengths)
MAX_IN_SHOT = 6
MAX_LINES = 8
MAX_SCENE_IMAGES = 3  # reference pictures attached to one clip
MAX_CAST = 8  # characters one video may use (the worker's planner takes no more)
TEXT_LIMITS = {"setting": 400, "time_of_day": 60, "action": 1500, "sound": 300}
CHARACTER_LIMITS = {"name": 60, "appearance": 1000, "outfit": 200, "voice": 150, "personality": 200, "locked": 300,
                    "never": 200}
# The story bible Gemini writes with the script: what every scene shares. `characters` are the ones it invented.
BIBLE_LIMITS = {"title": 120, "style": 300, "palette": 200, "setting": 400, "motif": 200}
BIBLE_CHARACTER_LIMITS = {"name": 60, "look": 400, "never": 200, "voice": 150}
# What the prompt itself says about a picked character; it wins over their saved fields, one field at a time
BIBLE_CAST_LIMITS = {"name": 60, "age": 80, "look": 300, "outfit": 200, "voice": 150}
MAX_AVOID = 300  # the "Avoid:" line of one clip prompt

# Spins and dance turns: Veo tends to keep the face toward the camera while the body turns, so these scenes say
# how a body turns and don't pin the face to the (front-facing) reference picture.
TURNING = re.compile(r"\b(spin|spins|spinning|spun|twirl\w*|pirouett\w*|turns? around|turning around|"
                     r"rotat\w*|whirl\w*|circl\w*|revolv\w*|danc\w*)\b", re.I)
TURN_NOTE = ("As anyone turns, their head, shoulders, hips and feet rotate together as one body: the face moves "
             "into profile, then the back of the head and hair face the camera, then the face comes back round. "
             "The head never stays facing the camera while the body turns, and never twists on its own. "
             "Natural human anatomy, smooth continuous motion.")

# The next scene is sent the previous clip's last frame, so it carries on in the same place and clothes.
CONTINUE_FROM = ("Reference image {n} is the last frame of the previous shot: carry straight on from it, keeping the "
                 "place, light and every person's clothes and colours exactly the same.")

# With "Lock characters", each scene is first drawn as a still from the pictures; the clip then starts from it.
STARTS_FROM = ("Reference image {n} is this shot's first frame: start the clip exactly from it, keeping the same "
               "people, clothes, place and light.")

# Quality rules that close every clip prompt: what Veo most often gets wrong when it isn't told.
FORMATS = {"9:16": "Vertical 9:16 video.", "16:9": "Widescreen 16:9 video."}
ONE_SHOT = "One continuous shot with no cuts or scene changes."
CONTINUITY = ("Throughout the clip every person and animal keeps the same face, hair, body, clothing and colours, "
              "and the number of people never changes; nobody appears, disappears or morphs. Objects, clothes and "
              "the place keep their state unless the action changes it. Everyone has exactly two arms and two hands.")
# Both hands busy and a gesture as well: Veo then grows a third hand
BOTH_HANDS = re.compile(r"\b(?:in|with) both hands\b|\bboth hands (?:on|around|holding|full)\b|\btwo-handed\b", re.I)
GESTURE = re.compile(r"\b(gestur\w*|point\w*|wav\w*|clap\w*|thumbs?[- ]up|beckon\w*|shak\w* (?:a|her|his) finger|"
                     r"wag\w* (?:a|her|his) finger|throws? (?:her|his) hands?|raises? (?:a|her|his) hand)\b", re.I)
REALISM = ("Realistic physics and natural anatomy: correct hands and fingers, feet planted on the ground, natural "
           "motion speed, stable background.")
ANIMATED_REALISM = "Consistent proportions and smooth, natural motion."
NO_TEXT = "No on-screen text, captions, subtitles, logos or watermarks; no written words on signs, flags or objects."
NO_TEXT_AVOID = "lettering on signs, flags or banners"
# Places where Veo paints a name or a sign unasked ("Biirya…" on a biryani stall's bunting)
SIGNAGE_PLACES = re.compile(r"\b(shops?|stalls?|stores?|markets?|bazaars?|restaurants?|caf[eé]s?|dhabas?|carts?|"
                            r"streets?|bunting|flags?|banners?|kiosks?|vendors?|hotels?|stations?)\b", re.I)
# An animated video: a person with no reference picture is drawn just as cartoon-like as the pictured ones
SAME_STYLE = "Everyone in the same {style} style, none more realistic."
# Whose line is whose: Veo otherwise lets the wrong mouth say a line, or two people say it together
SPEAKERS = "Each line is said only by the person named before it, in their voice; nobody else says or mouths it."
SPEAKERS_IN_ORDER = "Lines in the order written."
# Each clip would make up its own tune, so the music would jump at every cut of a multi-scene video.
NO_MUSIC = "No background music: only the spoken lines and natural sound effects."
MUSIC = re.compile(r"\b(music\w*|songs?|sings?|singing|melod\w*|soundtrack|beat|band|instrumental)\b", re.I)
# A style written in the prompt ("Style: 3D animated cartoon, cheerful"), used when the Style field is empty
STYLE_LINE = re.compile(r"^[\s*•-]*(?:visual\s+)?style\s*:[ \t]*(\S.*)$", re.I | re.M)
STYLE_WORDS = re.compile(r"\b(?:[23]d animat\w*(?: cartoon)?|animated cartoon|cartoon|anime|pixar[- ]style|claymation|"
                         r"stop[- ]motion|watercolou?r|hand[- ]drawn|photo-?realistic|realistic|cinematic|"
                         r"documentary|vintage|black[- ]and[- ]white)\b", re.I)
ANIMATED = re.compile(r"\b(anim\w*|anime|cartoon\w*|pixar|3d|clay\w*|stop[- ]motion|illustrat\w*|comic\w*)\b", re.I)
# The action asks for words on screen: a sign, a title, or text in quotes.
ON_SCREEN_TEXT = re.compile(r"\b(text|signs?|signboard|titles?|captions?|banners?|posters?|logos?|written|"
                            r"writing|reads|labels?)\b|(^|\s)['\"][^'\"]{2,}['\"]", re.I)
# A spoken line in the action ("Billa asks: "…""): words said aloud, not words on screen
SPOKEN_LINE = re.compile(r"\b(?:says|said|asks|asked|replies|replied|answers|shouts|yells|whispers|exclaims|adds|cries|"
                         r"calls|screams|mutters|sings|tells \w+)\s*:?\s*['\"“][^'\"”]{2,}['\"”]", re.I)


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


def ensure_cast(scene: dict, cast: list[dict], bible: dict | None = None) -> dict:
    """Keep the user's chosen characters in a scene Gemini wrote: one unmatched person in the shot while one chosen
    character is missing is taken to be them (Gemini renamed them), and a chosen character named in the action or
    the lines is put in the shot. Their library picture and words then go with the clip. Someone the bible says
    Gemini invented (the baby) is never taken for a chosen character, and is put in the shot when named, so their
    bible look goes with every clip they're in."""
    people = scene["characters"]
    invented = [c["name"] for c in (bible or {}).get("characters") or []]
    missing = [c for c in cast if c["id"] not in {p["id"] for p in people}]
    unmatched = [p for p in people if p["id"] is None and p["name"].lower() not in {n.lower() for n in invented}]
    if len(missing) == 1 and len(unmatched) == 1:
        old, c = unmatched[0]["name"], missing.pop()
        unmatched[0].update(id=c["id"], name=c["name"])
        for d in scene["dialogue"]:
            if d["id"] is None and d["name"] == old:
                d.update(id=c["id"], name=c["name"])
    text = " ".join([scene.get("action", "")] + [d["line"] for d in scene["dialogue"]])
    for c in missing:
        if len(people) < MAX_IN_SHOT and re.search(rf"\b{re.escape(c['name'])}\b", text, re.I):
            people.append({"id": c["id"], "name": c["name"], "position": ""})
    for name in invented:
        if len(people) < MAX_IN_SHOT and name.lower() not in {p["name"].lower() for p in people} \
                and re.search(rf"\b{re.escape(name)}\b", text, re.I):
            people.append({"id": None, "name": name, "position": ""})
    return scene


def per_shot(cast_size: int, scene_count: int) -> int:
    """How many people or animals one shot should hold: 3 (what keeps faces steady and the pictures one clip
    takes), more only when the picked cast can't otherwise all appear."""
    return min(max(MAX_SCENE_IMAGES, -(-cast_size // max(scene_count, 1))), MAX_IN_SHOT)


def is_structured(scene: dict) -> bool:
    """True when the scene has more than a bare action (an old-style prompt)."""
    return any(scene.get(k) for k in ("setting", "time_of_day", "sound", "characters", "dialogue")) or \
        any((scene.get("shot") or {}).values())


def turning(scene: dict) -> bool:
    """True when someone in the scene spins, twirls or dances."""
    return bool(TURNING.search(scene.get("action") or ""))


def _sentence(text: str) -> str:
    text = text.strip()
    text = text[:1].upper() + text[1:]
    return text if not text or text[-1] in ".!?'\"" else text + "."


def style_from_prompt(prompt: str) -> str:
    """The visual style a prompt asks for: its "Style:" line, else the style words in its opening, else ""."""
    m = STYLE_LINE.search(prompt or "")
    if m:
        return clean_text(m.group(1), 300)
    words = {}
    for w in STYLE_WORDS.findall((prompt or "")[:400]):
        words.setdefault(w.lower(), w)
    if any(ANIMATED.search(w) for w in words):  # "realistic 3D animation" means detailed, not photographic
        words = {k: w for k, w in words.items() if not re.fullmatch(r"(photo-?)?realistic", k)}
    return clean_text(", ".join(words.values()), 300)


# The shape a prompt asks for. "portrait" and "landscape" alone are a picture and scenery, not a format.
ASPECT_WORDS = {
    "9:16": re.compile(r"\b9\s*[:x×/]\s*16\b|\bvertical\b|\bportrait (?:video|format|mode|orientation)\b|"
                       r"\b(?:reels?|shorts|tiktok|stories)\b", re.I),
    "16:9": re.compile(r"\b16\s*[:x×/]\s*9\b|\bhorizontal\b|\blandscape (?:video|format|mode|orientation)\b|"
                       r"\bwide-?screen\b", re.I),
}


def aspect_from_prompt(prompt: str) -> str:
    """The aspect a prompt names ("9:16", "vertical", "16:9", "widescreen"…): a written ratio over a word, the
    earliest on a tie; "" when it names none, so the user's choice is used."""
    found = []
    for aspect, words in ASPECT_WORDS.items():
        for m in words.finditer(prompt or ""):
            found.append((not m.group(0)[0].isdigit(), m.start(), aspect))
    return min(found)[2] if found else ""


def _style_name(style: str) -> str:
    """The video's style as a short name for SAME_STYLE ("Pixar-style, cartoon" → "Pixar-style cartoon")."""
    name = re.sub(r"[.;]+.*$", "", (style or "").strip()).replace(", ", " ").strip()
    return name[:60] if name else "cartoon"


def wearing(outfit: str) -> str:
    """An outfit as said after "wearing": a script's own "wearing a white kameez" isn't doubled."""
    return re.sub(r"^\s*(?:is\s+)?wearing\s+", "", outfit or "", flags=re.I).strip()


def animated(scene: dict, style: str = "") -> bool:
    """True for a cartoon or animated look, named in the style or in the scene itself."""
    return bool(ANIMATED.search(" ".join([style, scene.get("setting") or "", scene.get("action") or ""])))


def quality_rules(scene: dict, style: str = "", music: bool = True) -> str:
    """The closing line of every clip prompt: one shot, the same people throughout, believable motion, no text,
    and (without `music`) no music, unless the scene itself asks for it."""
    cartoon = animated(scene, style)
    rules = [ONE_SHOT, CONTINUITY, ANIMATED_REALISM if cartoon else REALISM]
    if cartoon and len(scene.get("characters") or []) > 1:
        rules.append(SAME_STYLE.format(style=_style_name(style)))
    if not ON_SCREEN_TEXT.search(SPOKEN_LINE.sub(" ", scene.get("action") or "")):
        rules.append(NO_TEXT)
    if not music and not MUSIC.search(" ".join([scene.get("action") or "", scene.get("sound") or ""])):
        rules.append(NO_MUSIC)
    return " ".join(rules)


def normalize_bible(raw) -> dict:
    """The story bible as stored: known fields only, bounded; characters by unique name."""
    raw = raw if isinstance(raw, dict) else {}
    bible = {k: clean_text(raw.get(k), n) for k, n in BIBLE_LIMITS.items()}
    people, seen = [], set()
    for item in raw.get("characters") or []:
        if not isinstance(item, dict):
            continue
        c = {k: clean_text(item.get(k), n) for k, n in BIBLE_CHARACTER_LIMITS.items()}
        if c["name"] and c["name"].lower() not in seen and (c["look"] or c["never"]):
            seen.add(c["name"].lower())
            people.append(c)
    bible["characters"] = people[:MAX_CAST]
    cast, seen = [], set()
    for item in raw.get("cast") or []:
        if not isinstance(item, dict):
            continue
        c = {k: clean_text(item.get(k), n) for k, n in BIBLE_CAST_LIMITS.items()}
        if c["name"] and c["name"].lower() not in seen and any(c[k] for k in BIBLE_CAST_LIMITS if k != "name"):
            seen.add(c["name"].lower())
            cast.append(c)
    bible["cast"] = cast[:MAX_CAST]
    return bible


# An age in a saved appearance ("60 age", "aged 12", "in her 30s", "a 5-year-old"), dropped when the prompt gives one
AGE_WORDS = re.compile(r"\b(?:aged?\s*\d{1,3}|\d{1,3}\s*(?:-\s*)?(?:years?(?:[\s-]old)?|yrs?|age)|"
                       r"in (?:his|her|their) (?:early |mid |late )?\d0s|an? \d{1,3}-year-old)\b[\s,;]*", re.I)


def merged_sheet(sheet: dict, bible: dict | None) -> dict:
    """A picked character as this video shows them: each age, look, outfit or voice the prompt gives (the bible's
    cast entry) replaces their saved one; whatever the prompt doesn't say stays as saved. `from_prompt` names
    the prompt's own entry (only the fields it gives)."""
    name = (sheet.get("name") or "").lower()
    over = next((c for c in (bible or {}).get("cast") or [] if name and c["name"].lower() == name), None)
    if not over:
        return sheet
    out = {**sheet, "from_prompt": {k: v for k, v in over.items() if k != "name" and v}}
    if over.get("look") or over.get("age"):
        saved = sheet.get("appearance") or ""
        rest = over.get("look") or (AGE_WORDS.sub("", saved).strip(" ,;") if over.get("age") else saved)
        out["appearance"] = "; ".join(x for x in (f"age: {over['age']}" if over.get("age") else "", rest) if x)
    for k in ("outfit", "voice"):
        if over.get(k):
            out[k] = over[k]
    return out


def _avoid(items: list[str]) -> str:
    """The "Avoid:" line: each item once, in order, cut to MAX_AVOID."""
    seen, out = set(), []
    for item in items:
        for part in re.split(r"[,;]", item or ""):
            part = part.strip(" .")
            if part and part.lower() not in seen:
                seen.add(part.lower())
                out.append(part)
    return "Avoid: " + _fit(", ".join(out), MAX_AVOID) + "." if out else ""


def _fit(text: str, room: int) -> str:
    """`text` cut at a word to at most `room` characters."""
    if len(text) <= room:
        return text
    cut = text[:max(room, 0)]
    return cut[:cut.rfind(" ")].rstrip(" ,;:") if " " in cut else cut


def compile_prompt(scene: dict, cast: list[dict], style: str = "", language: str = "", aspect: str = "",
                   fit: bool = False, bible: dict | None = None, genre: str = "", music: bool = True,
                   room: int = MAX_PROMPT) -> str:
    """The clip prompt for one scene: format, style and palette, setting, camera, who is where (their reference
    picture and locked traits, else their library or bible words), action, lines in each character's voice, sound,
    what to avoid, then the quality rules. A scene that is only an action (and no style) keeps its words as written.
    With `fit`, the action is shortened so the whole prompt fits in MAX_PROMPT and the rules are never cut off.
    `bible`: the video's story bible; `genre`: its type (app/genres.py); `music`: allow a soundtrack in the clip.
    `room`: the length `fit` keeps it within (less than MAX_PROMPT when lines are added when it's sent)."""
    by_id = {c["id"]: c for c in cast}
    bible = bible or {}
    invented = {c["name"].lower(): c for c in bible.get("characters") or []}
    style = style or bible.get("style") or ""
    plain = not is_structured(scene) and not style
    head, tail, avoid = [], [], clip_avoid(genre)
    if aspect in FORMATS:
        head.append(FORMATS[aspect])
    if style:
        head.append(_sentence(style))
    if bible.get("palette"):
        head.append(_sentence(f"Colour palette: {bible['palette']}"))
    setting = ", ".join(x.rstrip(" .,;") for x in (scene.get("setting"), scene.get("time_of_day")) if x)
    if setting:
        head.append(_sentence(setting))
    shot = scene.get("shot") or {}
    framing = shot.get("framing", "")
    if framing and "shot" not in framing:
        framing += " shot"
    camera = ", ".join(x for x in (framing, shot.get("angle"), shot.get("movement"), shot.get("lens")) if x)
    if camera:
        head.append(_sentence(camera))
    turns = turning(scene)
    people, pictured = [], 0
    for p in scene.get("characters") or []:
        sheet = merged_sheet(by_id[p["id"]], bible) if p.get("id") in by_id else invented.get(p["name"].lower()) or {}
        where = f", {p['position']}" if p.get("position") else ""
        told = sheet.get("from_prompt") or {}
        # what the prompt says about a pictured character, after their picture (a new outfit replaces the picture's)
        said = "".join(x for x in (f", age: {told['age']}" if told.get("age") else "",
                                   f", {_fit(told['look'], 200)}" if told.get("look") else "",
                                   f", wearing {wearing(told['outfit'])}" if told.get("outfit") else ""))
        locked = f"; always {sheet['locked'].rstrip(' .')}" if sheet.get("locked") else ""
        avoid.append(sheet.get("never") or "")
        look = "; ".join(x.strip(" .,;") for x in (sheet.get("appearance"), sheet.get("outfit"), sheet.get("look"))
                         if x and x.strip(" .,;"))
        if sheet.get("image_path") and pictured < MAX_SCENE_IMAGES:  # numbered as scene_images attaches them
            pictured += 1  # the picture carries their look, not their saved words (which could pull away from it)
            # what the prompt describes (age or look → hair and build; outfit) comes from the prompt, not the picture
            kept = ["face", *(["hair"] if not (told.get("age") or told.get("look")) else []),
                    *(["outfit"] if not told.get("outfit") else []), "style"]
            same = ", ".join(kept[:-1]) + " and " + kept[-1]
            if turns:  # the picture fixes who they are, not which way they face
                people.append(_sentence(f"{p['name']}{where}: same person, {same.replace('face, ', '')} as reference "
                                        f"image {pictured}{said}; the picture only shows them from the front, so "
                                        f"show them from any side as they move{locked}"))
            elif said:
                people.append(_sentence(f"{p['name']}{where}, the same {same} as reference image {pictured}"
                                        f"{said}{locked}"))
            else:
                people.append(_sentence(f"{p['name']}{where}, exactly as in reference image {pictured} "
                                        f"(same face, hair, outfit and style){locked}"))
            continue
        people.append(_sentence(p["name"] + (f" ({look})" if look else "") + where + locked))
    if people:
        head.append("Characters: " + " ".join(people))
    action = scene.get("action") or ""
    if turns:
        tail.append(TURN_NOTE)
    lang = f" in {language}" if language else ""
    for d in scene.get("dialogue") or []:
        voice = merged_sheet(by_id[d["id"]], bible).get("voice") if d.get("id") in by_id \
            else (invented.get(d["name"].lower()) or {}).get("voice") or ""
        who = d["name"] + (f" (voice: {voice})" if voice else "")
        tail.append(f"{who} says{lang}: '{d['line']}'")
    if scene.get("sound"):
        tail.append(f"Sound: {_sentence(scene['sound'])}")
    rules = quality_rules(scene, style, music)
    if scene.get("dialogue"):
        speakers = {d["name"] for d in scene["dialogue"]}
        tail.append("Only the named characters speak, clearly and in sync with their lips."
                    + ("" if NO_TEXT in rules else " No subtitles.") + " " + SPEAKERS
                    + (" " + SPEAKERS_IN_ORDER if len(scene["dialogue"]) > 1 and len(speakers) > 1 else ""))
    if NO_TEXT in rules and SIGNAGE_PLACES.search(" ".join([scene.get("setting") or "", action])):
        avoid.append(NO_TEXT_AVOID)
    if _avoid(avoid):
        tail.append(_avoid(avoid))
    tail.append(rules)
    if action and fit:
        action = _fit(action if plain else _sentence(action), room - len("\n".join(head + tail)) - 1)
    if action:
        head.append(action if plain or fit else _sentence(action))
    return "\n".join(head + tail)


def still_prompt(scene: dict, cast: list[dict], style: str = "", bible: dict | None = None) -> str:
    """What a scene's locked first frame shows: the look, the place, the camera, who is where (the pictured ones by
    their picture's number, as scene_images orders them) and the first moment of the action. Everyone is described
    as the clip prompt describes them (compile_prompt): what the prompt says about a person (age, look, outfit) wins
    over their saved character and picture, so the frame and the clip agree and nothing changes mid-clip."""
    by_id, bible = {c["id"]: c for c in cast}, bible or {}
    invented = {c["name"].lower(): c for c in bible.get("characters") or []}
    lines = [_sentence(x) for x in (style or bible.get("style") or "",
                                    f"Colour palette: {bible['palette']}" if bible.get("palette") else "") if x]
    place = ", ".join(x.rstrip(" .,;") for x in (scene.get("setting"), scene.get("time_of_day")) if x)
    if place:
        lines.append(_sentence(place))
    framing = (scene.get("shot") or {}).get("framing")
    if framing:
        lines.append(_sentence(f"{framing} shot" if "shot" not in framing else framing))
    people, pictured = [], 0
    for p in scene.get("characters") or []:
        sheet = merged_sheet(by_id[p["id"]], bible) if p.get("id") in by_id else invented.get(p["name"].lower()) or {}
        where = f", {p['position']}" if p.get("position") else ""
        locked = f"; always {sheet['locked'].rstrip(' .')}" if sheet.get("locked") else ""
        if sheet.get("image_path") and pictured < MAX_SCENE_IMAGES:
            pictured += 1
            told = sheet.get("from_prompt") or {}
            said = "".join(x for x in (f", age: {told['age']}" if told.get("age") else "",
                                       f", {_fit(told['look'], 200)}" if told.get("look") else "",
                                       f", wearing {wearing(told['outfit'])} (not the clothes in the picture)"
                                       if told.get("outfit") else ""))
            people.append(_sentence(f"{p['name']}{where} (picture {pictured}){said}{locked}"))
        else:
            look = "; ".join(x.strip(" .,;") for x in (sheet.get("appearance"), sheet.get("outfit"), sheet.get("look"))
                             if x and x.strip(" .,;"))
            people.append(_sentence(p["name"] + (f" ({look})" if look else "") + where + locked))
    if people:
        lines.append("Characters: " + " ".join(people))
    first = re.split(r"(?<=[.!?])\s", (scene.get("action") or "").strip(), maxsplit=1)[0]
    if first:
        lines.append(_sentence(f"The first moment: {first}"))
    return _fit("\n".join(lines), MAX_PROMPT - 600)  # room for the worker's own lines


def spoken_words(scene: dict) -> int:
    return sum(len(d["line"].split()) for d in scene.get("dialogue") or [])


def busy_hands(scene: dict) -> bool:
    """True when someone holds something in both hands and is also told to point, wave or gesture."""
    action = scene.get("action") or ""
    return bool(BOTH_HANDS.search(action) and GESTURE.search(action))


def words_per_clip(seconds: int = 10) -> int:
    """About how many spoken words fit in a `seconds`-long clip: 25 in Gemini's 10 s, 20 in Flow's 8 s."""
    return round(MAX_SPOKEN_WORDS * seconds / 10)


def scene_warnings(scene: dict, prompt: str, cast: list[dict] = (), seconds: int = 10) -> list[str]:
    out = []
    if busy_hands(scene):
        out.append("Someone holds something in both hands and also points, waves or gestures: the video may grow "
                   "a third hand. Say which hand does what (e.g. 'holds the roti in her left hand, points with her "
                   "right').")
    words = spoken_words(scene)
    if words > words_per_clip(seconds):
        out.append(f"{words} spoken words: about {words_per_clip(seconds)} fit in {seconds} seconds, so lines may be "
                   "cut short.")
    if len(prompt) > MAX_PROMPT:
        out.append(f"The clip prompt is {len(prompt):,} characters and Gemini takes {MAX_PROMPT:,}, so the end of the "
                   f"action (about {len(prompt) - MAX_PROMPT + 1:,} characters) is cut when it's sent. Shorten the "
                   "action or setting a little to keep all of it.")
    pictured = {c["id"] for c in cast if c.get("image_path")}
    n = sum(1 for p in scene.get("characters") or [] if p.get("id") in pictured)
    if n > MAX_SCENE_IMAGES:
        out.append(f"{n} pictured characters: only {MAX_SCENE_IMAGES} pictures go with a clip, so the rest may not "
                   "look the same. Keep 3 or fewer people in this shot.")
    return out


def scene_images(scene: dict, cast: list[dict], fallback: list[str], limit: int = MAX_SCENE_IMAGES) -> list[str]:
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
    sheet = {k: c.get(k) or "" for k in ("name", "appearance", "outfit", "voice", "personality")}
    if c.get("locked"):  # so the script never contradicts what must stay the same
        sheet["appearance"] = "; ".join(x for x in (sheet["appearance"], f"always {c['locked']}") if x)
    return sheet
