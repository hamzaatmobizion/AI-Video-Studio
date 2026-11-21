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
FLOW_MAX_PROMPT = 3300  # Google Flow follows a longer one to its last line (a 3,298-character prompt was tested)
# What Flow's prompt box takes (a 22,800-character prompt was made; 28,800 was refused): only for a user's own full
# prompt sent as written (as_written_prompt), whose structure carries it; the app's own prompts stay at FLOW_MAX_PROMPT
FLOW_INPUT_LIMIT = 30000
FLOW_TESTED = 22000  # past this a brief goes without our rules block (it has its own), to stay near what Flow made
AS_WRITTEN_MIN = 1500  # a one-clip Flow prompt this long is a full brief: sent as written, not rewritten by Gemini
BRIEF_MIN = 3000  # a longer Flow video's prompt this long, with timed beats for every clip: split and sent as written
MAX_SPOKEN_WORDS = 25  # about what fits in one 10-second Gemini clip (words_per_clip for other lengths)
# Google Flow's 8-second clip has about 6 seconds for speech once the start and the closing reaction have theirs:
# 20 words ran the lines back to back with no pause. Each change of speaker also needs a short gap.
FLOW_SECONDS = 8
FLOW_WORDS = {"one": 16, "more": 14}  # spoken words with one speaker, and with two or more
FLOW_MOST_WORDS = 20  # more than this in one 8-second clip and the lines run together (tight up to it)
# Writing without spaces between words (Chinese, Japanese, Thai, Lao, Khmer, Burmese): 2 characters count as a word
NO_SPACE_SCRIPT = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿฀-໿က-႟"
                             r"ក-៿]")
CHARS_PER_WORD = 2
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
# The objects the story keeps coming back to (the robot, the tea cup): their fixed look in the bible, and in each
# scene where each one is and who holds it in which hand, so no clip redraws or drops them
BIBLE_PROP_LIMITS = {"name": 60, "look": 200}
PROP_STATE_LIMITS = {"name": 60, "state": 200}
MAX_PROPS = 8
MAX_PROPS_LINE = 800  # the "Objects" line of one clip prompt
MAX_AVOID = 300  # the "Avoid:" line of one clip prompt

# Spins and dance turns: Veo tends to keep the face toward the camera while the body turns, so these scenes say
# how a body turns and don't pin the face to the (front-facing) reference picture.
TURNING = re.compile(r"\b(spin|spins|spinning|spun|twirl\w*|pirouett\w*|turns? around|turning around|"
                     r"rotat\w*|whirl\w*|circl\w*|revolv\w*|danc\w*)\b", re.I)
TURN_NOTE = ("As anyone turns, their head, shoulders, hips and feet rotate together as one body: the face moves "
             "into profile, then the back of the head and hair face the camera, then the face comes back round. "
             "The head never stays facing the camera while the body turns, and never twists on its own. "
             "Natural human anatomy, smooth continuous motion.")
TURN_NOTE_SHORT = "Anyone who turns turns head, shoulders, hips and feet together as one body."

# The next scene is sent the previous clip's last frame, so it carries on in the same place and clothes.
CONTINUE_FROM = ("Reference image {n} is the last frame of the previous shot: carry straight on from it, keeping the "
                 "place, light and every person's clothes and colours exactly the same.")

# A by-hand Flow scene made in Frames mode: the previous clip's last frame is its Start frame (no reference pictures)
FRAME_START = ("The clip starts exactly from the start frame, the last frame of the previous shot, and carries straight "
               "on from it, keeping the place, light and every person's face, clothes and colours exactly the same; "
               "the action begins naturally from where everyone stands in it.")

# A Flow scene that carries on from the previous clip's last frame, when its people differ from that frame's: Veo
# otherwise keeps whoever the frame shows and never brings in someone new
LEAVE_SHOT = "Not in this shot: {names}, who leave the frame in the first second and don't come back."
ENTER_SHOT = "New in this shot: {names}, who walk into the frame in the first two seconds and stay to the end."
# The next clip starts from this one's last frame: whoever is in both stays in view to the end
KEEP_IN_FRAME = "End the clip with {names} all clearly in frame: the next clip starts from its last frame."
# …and whoever the next clip doesn't have is out of it by then
LEAVE_BY_END = "{names} {leave} the frame naturally before the end and {are} not in the last frame."

# With "Lock characters", each scene is first drawn as a still from the pictures; the clip then starts from it.
STARTS_FROM = ("Reference image {n} is this shot's first frame: start the clip exactly from it, keeping the same "
               "people, clothes, place and light.")

# Quality rules that close every clip prompt: what Veo most often gets wrong when it isn't told.
FORMATS = {"9:16": "Vertical 9:16 video.", "16:9": "Widescreen 16:9 video."}
ONE_SHOT = "One continuous shot with no cuts or scene changes."
CONTINUITY = ("Throughout the clip every person and animal keeps the same face, hair, body, clothing and colours, "
              "and the number of people never changes; nobody appears, disappears or morphs. Objects, clothes and "
              "the place keep their state unless the action changes it. Everyone has exactly two arms and two hands.")
# The same rules in fewer words, for a clip prompt that would otherwise have to cut its action
CONTINUITY_SHORT = ("Everyone keeps the same face, hair, body, clothes and colours; objects keep their look and state; "
                    "nobody appears or disappears. Everyone has exactly two arms and two hands.")
# Both hands busy and a gesture as well: Veo then grows a third hand
BOTH_HANDS = re.compile(r"\b(?:in|with) both hands\b|\bboth hands (?:on|around|holding|full)\b|\btwo-handed\b", re.I)
GESTURE = re.compile(r"\b(gestur\w*|point\w*|wav\w*|clap\w*|thumbs?[- ]up|beckon\w*|shak\w* (?:a|her|his) finger|"
                     r"wag\w* (?:a|her|his) finger|throws? (?:her|his) hands?|raises? (?:a|her|his) hand)\b", re.I)
# Holding something and clapping: Veo claps with both hands and leaves the thing standing or floating on its own
# (a Flow clip's "Objects" rule, flow_rules, says what is held stays in hand)
HOLDING = re.compile(r"\b(?:holds?|holding|held|grips?|gripping|clutch\w*|carr(?:y|ies|ying))\b|"
                     r"\bin (?:her|his|their|one) (?:right|left) hand\b", re.I)
CLAPPING = re.compile(r"\b(?:clap\w*|applau\w*)\b", re.I)
REALISM = ("Realistic physics and natural anatomy: correct hands and fingers, feet planted on the ground, natural "
           "motion speed, stable background.")
ANIMATED_REALISM = "Consistent proportions and smooth, natural motion."
# The master rules' camera, physics, light and motion-continuity lines, in as few words as Veo needs
CINEMATIC = ("Smooth, controlled camera with no shaking, random zooms or sudden angle changes. Real-world gravity, "
             "weight and contact: nothing floats or passes through anything. Movement flows on with no jumps, "
             "teleporting or slow motion. Lighting keeps the same direction and brightness, with no flicker. Only "
             "what the action asks for happens: no extra people, objects or events.")
# The same for a cartoon, from the 3D cartoon rules: cartoon physics, stable models, and never a switch of style
CINEMATIC_CARTOON = ("Steady camera with consistent perspective; no shaking, random zooms or sudden angle changes. "
                     "Believable cartoon physics and natural contact; nothing floats. Smooth transitions between "
                     "expressive, appealing poses, with no jumps or slow motion. Models, textures and shapes never "
                     "deform or flicker; lighting stays constant. One {style} look throughout, never photorealism or "
                     "another art style. Nothing extra that the action doesn't ask for.")
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
SPEAKERS_SHORT = "Only the named characters speak, in sync with their lips, each line by the person named before it."
# How every clip should look and perform, whatever its story: said in every clip prompt and never shortened away
LIP_SYNC = ("Lip-sync: the speaker's mouth shapes match every syllable of the line, starting and stopping exactly with "
            "the words, with natural pauses between lines so nobody rushes.")
ACTING = ("Acting: expressive, believable performances; faces, eyes and eyebrows clearly show each feeling and match the "
          "words; natural blinking and eye contact; whoever listens reacts with their mouth closed; the key or funny "
          "moment gets a clear, held reaction.")
QUALITY_ANIMATED = ("Premium animated feature-film quality: detailed skin, hair and fabric textures, soft natural "
                    "lighting and shadows, smooth fluid animation, correct hands and fingers, clean frames with no "
                    "flicker or glitches.")
QUALITY_FILM = ("Premium cinematic quality: sharp focus, natural skin and fabric detail, soft natural lighting, smooth "
                "natural motion, correct hands and fingers, clean frames with no flicker or glitches.")
# What a "Style:" line may also say that isn't a look ("vertical 9:16, 40 seconds"): each clip has its own format
FORMAT_WORDS = re.compile(r"\b\d+\s*[:x×/]\s*\d+\b|\bvertical\b|\bhorizontal\b|\bwide-?screen\b|\bportrait (?:video|"
                          r"format|mode)\b|\blandscape (?:video|format|mode)\b|\b\d+[\s-]?(?:secs?|seconds?|minutes?|"
                          r"mins?)\b|\bduration\b|\b(?:4k|1080p|720p|\d+\s*fps)\b", re.I)
# Each clip would make up its own tune, so the music would jump at every cut of a multi-scene video.
NO_MUSIC = "No background music: only the spoken lines and natural sound effects."
MUSIC = re.compile(r"\b(music\w*|songs?|sings?|singing|melod\w*|soundtrack|beat|band|instrumental)\b", re.I)
# A style written in the prompt ("Style: 3D animated cartoon, cheerful"), used when the Style field is empty
STYLE_LINE = re.compile(r"^[\s*•-]*(?:visual\s+)?style\s*:[ \t]*(\S.*)$", re.I | re.M)
STYLE_WORDS = re.compile(r"\b(?:[23]d (?:animat\w*(?: cartoon)?|cartoon(?: animat\w*)?)|animated cartoon|cartoon|anime|pixar[- ]style|claymation|"
                         r"stop[- ]motion|watercolou?r|hand[- ]drawn|photo-?realistic|realistic|cinematic|"
                         r"documentary|vintage|black[- ]and[- ]white)\b", re.I)
ANIMATED = re.compile(r"\b(anim\w*|anime|cartoon\w*|pixar|3d|clay\w*|stop[- ]motion|illustrat\w*|comic\w*)\b", re.I)
# The action asks for words on screen: a sign, a title, or text in quotes.
ON_SCREEN_TEXT = re.compile(r"\b(text|signs?|signboard|titles?|captions?|banners?|posters?|logos?|written|"
                            r"writing|reads|labels?)\b|(^|\s)['\"][^'\"]{2,}['\"]", re.I)
# A spoken line in the action ("Billa asks: "…""): words said aloud, not words on screen
SPEECH_VERBS = (r"says|said|asks|asked|replies|replied|answers|answered|shouts|shouted|yells|yelled|whispers|"
                r"whispered|exclaims|exclaimed|adds|added|cries|cried|calls|called|screams|screamed|mutters|"
                r"muttered|sings|sang|tells \w+|speaks|says to \w+|asks \w+")
PRONOUNS = ("she", "he")
# Birdsong in a sound list ("birds singing", "songbirds"): a sound effect, not a request for music
BIRDSONG = re.compile(r"\b(?:song)?birds?\b[^.,;]{0,20}?\b(?:sing\w*|song\w*|chirp\w*)|\bsinging\s+birds?\b|"
                      r"\bbird\s*songs?\b|\bsongbirds?\b", re.I)
# A pictured character who is an animal: described by their fur and look, not hair and outfit
ANIMAL = re.compile(r"\b(?:rabbit|bunny|cat|kitten|dog|puppy|goat|cow|buffalo|horse|donkey|sheep|lamb|parrot|bird|"
                    r"hen|chicken|rooster|duck|monkey|lion|tiger|elephant|bear|fox|owl|mouse|rat|camel|deer|"
                    r"squirrel|frog|turtle|panda|penguin)s?\b", re.I)
# A clip's camera that doesn't move: "static", "still" or "locked-off", with no push-in, pan or tracking too
STATIC = re.compile(r"\b(?:static|still|locked[- ]off|fixed)\b", re.I)
CAMERA_MOVES = re.compile(r"\b(?:push|pull|pan|tilt|track|dolly|zoom|crane|orbit|follow|handheld|ending close|"
                          r"moves?|moving)\w*", re.I)
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
    scene["props"] = _named(raw.get("props"), PROP_STATE_LIMITS, "name")
    return scene


def _named(items, limits: dict, needed: str) -> list[dict]:
    """A list of {name, …} dicts, bounded, by unique name, keeping only those with a `needed` field."""
    out, seen = [], set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        c = {k: clean_text(item.get(k), n) for k, n in limits.items()}
        if c["name"] and c[needed] and c["name"].lower() not in seen:
            seen.add(c["name"].lower())
            out.append(c)
    return out[:MAX_PROPS]


# A person an action is about, when it names nobody ("the little boy", "a woman"): taken to be the one chosen character
SOMEONE = re.compile(r"\b(boy|girl|man|woman|child|kid|lady|guy|person|teen(?:ager)?|baby|toddler|son|daughter|father|"
                     r"mother|dad|mom|mum|grand(?:father|mother|pa|ma)|uncle|aunt|student|farmer|old man|old woman)s?\b",
                     re.I)


def ensure_cast(scene: dict, cast: list[dict], bible: dict | None = None) -> dict:
    """Keep the user's chosen characters in a scene Gemini wrote: one unmatched person in the shot while one chosen
    character is missing is taken to be them (Gemini renamed them), and a chosen character named in the action or
    the lines is put in the shot. A shot that lists nobody while its action is about a person ("the little boy
    dances") has the one chosen character, when just one was chosen. Their library picture and words then go with
    the clip. Someone the bible says Gemini invented (the baby) is never taken for a chosen character, and is put in
    the shot when named, so their bible look goes with every clip they're in."""
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
    if not people and len(cast) == 1 and not invented and SOMEONE.search(scene.get("action", "")):
        c = missing.pop()
        people.append({"id": c["id"], "name": c["name"], "position": ""})
    for c in missing:
        if len(people) < MAX_IN_SHOT and re.search(rf"\b{re.escape(c['name'])}\b", text, re.I):
            people.append({"id": c["id"], "name": c["name"], "position": ""})
    for name in invented:
        if len(people) < MAX_IN_SHOT and name.lower() not in {p["name"].lower() for p in people} \
                and re.search(rf"\b{re.escape(name)}\b", text, re.I):
            people.append({"id": None, "name": name, "position": ""})
    return scene


def include_cast(scenes: list[dict], cast: list[dict]) -> list[dict]:
    """Every chosen character in the video: one the script left out (no role in the prompt fitted them) goes into the
    scene with the fewest chosen characters that has room, so their picture is given with that clip."""
    for c in cast:
        if any(p["id"] == c["id"] for sc in scenes for p in sc["characters"]):
            continue
        room = [sc for sc in scenes if len(sc["characters"]) < MAX_IN_SHOT]
        if room:
            min(room, key=lambda sc: sum(p["id"] is not None for p in sc["characters"]))["characters"].append(
                {"id": c["id"], "name": c["name"], "position": ""})
    return scenes


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
        return look_only(clean_text(m.group(1), 300))
    words = {}
    for w in STYLE_WORDS.findall((prompt or "")[:400]):
        words.setdefault(w.lower(), w)
    if any(ANIMATED.search(w) for w in words):  # "realistic 3D animation" means detailed, not photographic
        words = {k: w for k, w in words.items() if not re.fullmatch(r"(photo-?)?realistic", k)}
    return look_only(clean_text(", ".join(words.values()), 300))


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


def look_only(style: str) -> str:
    """A style without the format, length or resolution a "Style:" line may add ("…, vertical 9:16, 40 seconds"):
    each clip prompt says its own format, and a clip told "40 seconds" is still 8 or 10."""
    parts = [p.strip(" .") for p in re.split(r"[,;]", style or "")]
    if not any(FORMAT_WORDS.search(p) for p in parts):
        return style or ""
    return ", ".join(p for p in parts if p and not FORMAT_WORDS.search(p))


def _style_name(style: str) -> str:
    """The video's style as a short name for SAME_STYLE ("Pixar-style, cartoon" → "Pixar-style cartoon"), cut at a
    whole word. A long sentence ("Beautiful high-quality Pixar-style 3D cartoon animation with vibrant…") becomes
    its style words alone ("Pixar-style cartoon"), so "one … look for everyone" reads as one."""
    name = re.sub(r"[.;]+.*$", "", (style or "").strip()).replace(", ", " ").strip()
    words = list(dict.fromkeys(m.group(0) for m in STYLE_WORDS.finditer(name)))
    if len(name) > 40 and words:
        return " ".join(words)
    if len(name) > 60:
        name = name[:61].rsplit(" ", 1)[0]
    return name or "cartoon"


def wearing(outfit: str) -> str:
    """An outfit as said after "wearing": a script's own "wearing a white kameez" isn't doubled."""
    return re.sub(r"^\s*(?:is\s+)?wearing\s+", "", outfit or "", flags=re.I).strip()


def animated(scene: dict, style: str = "") -> bool:
    """True for a cartoon or animated look, named in the style or in the scene itself."""
    return bool(ANIMATED.search(" ".join([style, scene.get("setting") or "", scene.get("action") or ""])))


def quality_rules(scene: dict, style: str = "", music: bool = True, short: bool = False,
                  cinematic: bool = True) -> str:
    """The closing line of every clip prompt: one shot, the same people throughout, believable motion, the master
    rules' camera and physics line, no text, and (without `music`) no music, unless the scene itself asks for it.
    `short`: in fewer words, for a prompt that would otherwise cut its action; it and `cinematic=False` leave out the
    camera and physics line."""
    cartoon = animated(scene, style)
    rules = [ONE_SHOT, CONTINUITY_SHORT] if short else [ONE_SHOT, CONTINUITY, ANIMATED_REALISM if cartoon else REALISM]
    if cinematic and not short:
        rules.append(CINEMATIC_CARTOON.format(style=_style_name(style)) if cartoon else CINEMATIC)
    if cartoon and len(scene.get("characters") or []) > 1:
        rules.append(SAME_STYLE.format(style=_style_name(style)))
    if not ON_SCREEN_TEXT.search(SPOKEN_LINE.sub(" ", scene.get("action") or "")):
        rules.append(NO_TEXT)
    if not music and not MUSIC.search(" ".join([scene.get("action") or "", scene.get("sound") or ""])):
        rules.append(NO_MUSIC)
    return " ".join(rules)


def speak_inline(action: str, dialogue: list[dict], sides: dict[str, str], lang: str = "") -> tuple[str, set[int]]:
    """The action with each spoken line back where the prompt had it ("Bahu puts her hands on her hips and says."
    → "Bahu (frame left) puts her hands on her hips and says: "…""), the speaker's place in the frame next to their
    name, and the indexes of the lines it placed. Veo then knows when each line is said and by whom: listed after
    the action, lines drifted from the lips and went to the wrong mouth. A line with no such spot is left out."""
    placed, pos = set(), 0
    for i, d in enumerate(dialogue):
        who = "|".join([re.escape(d["name"]), *PRONOUNS])
        m = re.compile(rf"(?<!\w)(?P<who>{who})(?!\w)(?P<lead>[^.!?:\"“”]{{0,300}}?\b(?:{SPEECH_VERBS})"
                       rf"(?:\s+[^\W\d_]+ly)?)\.", re.I).search(action, pos)
        if not m:
            continue
        side = sides.get(d["name"].lower())
        named = m.group("who")
        if named.lower() in PRONOUNS:
            named += f" ({d['name']}, {side})" if side else f" ({d['name']})"
        elif side:
            named += f" ({side})"
        said = f'{named}{m.group("lead")}{lang}: "{d["line"]}"'
        action = action[:m.start()] + said + action[m.end():]
        pos = m.start() + len(said)
        placed.add(i)
    return action, placed


def as_written_prompt(prompt: str, pictured: list[str], scene: dict, style: str = "", music: bool = True) -> str:
    """A one-clip Flow video's clip prompt from a full brief (the buffalo sarpanch prompt): the user's own text as
    written, with its layout, beats, dialogue lock and sound timeline, which a Gemini rewrite boiled down to a
    paragraph. Only what it can't know is added: which reference picture is whom (`pictured`, in scene_images
    order), and the short Flow rules."""
    head = []
    if pictured:
        head.append("Reference images: " + ", ".join(f"{i} = {n}" for i, n in enumerate(pictured, 1))
                    + "; each looks exactly as in their picture (face, hair, outfit and style).")
    # its own lines are in quotes even when the script missed them: the rules mustn't say "nobody speaks"
    spoken = scene.get("dialogue") or ([{"name": "", "line": "…"}] if re.search(r"[\"“”]", prompt) else [])
    rules = flow_rules({**scene, "action": prompt, "dialogue": spoken}, style, music)
    if len(prompt) + len("\n".join(head + rules + BRIEF_ESSENTIALS)) > FLOW_TESTED:  # it has its own rules
        rules = []
    return "\n".join([prompt.strip(), "", *head, *BRIEF_ESSENTIALS, *rules[1:]])


# Said after every brief, even one near Flow's limit (video 164: the buffalo stood behind Chacha from the first frame
# instead of walking in, and the camera jumped from close to wide; the brief's own words alone didn't hold)
BRIEF_ESSENTIALS = [
    "Start: the first frame shows only what the first beat describes; anyone who arrives later is not in the shot "
    "until they walk in.",
    "Places: everyone stands where the beats place them (left, right, centre), side by side, never one behind "
    "another.",
    "Shot: one continuous take; the camera moves only as the beats say, smoothly, with no cuts, jumps, sudden "
    "reframes or zooms.",
]


def flow_rules(scene: dict, style: str = "", music: bool = True) -> list[str]:
    """The rules that close a Flow clip prompt: one short line each for the shot and camera, the people, the
    objects, how things move, who speaks and how it looks. Veo follows a few plain lines better than paragraphs,
    and they leave the action its room."""
    cartoon = animated(scene, style)
    action = scene.get("action") or ""
    camera = " ".join([(scene.get("shot") or {}).get("movement") or "",
                       *re.findall(r"camera\s*:\s*([^.\n]*)", action, re.I)])
    if STATIC.search(camera) and not CAMERA_MOVES.search(camera):  # faces stay the same size: lips are easier to sync
        lines = ["Shot: one continuous shot, no cuts; camera completely still."]
    else:
        lines = ["Shot: one continuous shot, no cuts; steady camera, only the move above."]
    people = [p["name"] for p in scene.get("characters") or []]
    if people or scene.get("dialogue"):
        # what Veo adds when someone turns or the place is busy (video 156: a gajra, a nose pin, a second braid,
        # a stranger's hand at the edge); the count keeps out extra main characters, not a bazaar's passers-by
        count = (f"exactly {len(people)} main character{'s' if len(people) > 1 else ''}: {', '.join(people)}, "
                 "with background people only if the place has them; ") if people else ""
        # …except what the story changes (video 169: the goat ate the fake moustache, and it stayed on)
        lines.append(f"People: {count}same face, hair, jewellery and clothes, even when turning, except what the "
                     "action changes; add nothing (no flowers in the hair, nose pin, extra braid); all fully in "
                     "frame, none cut off at the edges; no stray hands; five fingers; expressive acting.")
    # video 158: two steering wheels
    lines.append("Objects: each only once (one door, one cup); they stay put unless moved; held things stay in hand; "
                 "nothing floats.")
    lines.append("Motion: smooth cartoon physics with squash and stretch and follow-through; natural walks and runs; "
                 "clothes and hair move naturally; no slow motion." if cartoon
                 else "Motion: real weight and speed, feet on the ground, no slow motion.")
    if scene.get("dialogue"):
        lines.append("Speech: only the named person speaks, lips in sync; listeners' mouths closed.")
    elif people:  # with no lines Veo may make someone mumble or chat
        lines.append("Speech: nobody speaks; laughs and giggles are fine.")
    look = [f"one {_style_name(style)} look, never photorealistic" if cartoon else "", "steady light, no flicker"]
    if not ON_SCREEN_TEXT.search(SPOKEN_LINE.sub(" ", action)):
        look.append("no text, subtitles, logos or writing anywhere")
    elif scene.get("dialogue"):
        look.append("no subtitles")
    heard = BIRDSONG.sub(" ", " ".join([action, scene.get("sound") or ""]))  # video 159: "birds singing" isn't music
    if not music and not MUSIC.search(heard):
        look.append("no music")
    lines.append("Look: " + "; ".join(x for x in look if x) + ".")
    return lines


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
    bible["props"] = _named(raw.get("props"), BIBLE_PROP_LIMITS, "look")
    return bible


def prompt_limit(provider: str = "") -> int:
    """How long one clip prompt may be where the clips are made: Google Flow takes more than Gemini."""
    return FLOW_MAX_PROMPT if provider == "flow" else MAX_PROMPT


def props_line(scene: dict, bible: dict | None = None, start_only: bool = False) -> str:
    """The "Objects" line of a clip prompt: each object the scene lists, with its fixed look from the bible and where
    it is when the clip starts or who holds it, so every clip draws it the same way. A bible object the action only
    names (a peel thrown away during the clip) gets its look alone: it isn't there from the start."""
    looks = {p["name"].lower(): p["look"].rstrip(" .") for p in (bible or {}).get("props") or []}
    states = {p["name"].lower(): p for p in scene.get("props") or []}
    text = " ".join([scene.get("action") or ""] + [d["line"] for d in scene.get("dialogue") or []])
    named = [p["name"] for p in (bible or {}).get("props") or []
             if p["name"].lower() not in states and re.search(rf"(?<!\w){re.escape(p['name'])}(?!\w)", text, re.I)]
    items = []
    for p in scene.get("props") or []:
        look, state = looks.get(p["name"].lower(), ""), p.get("state", "").rstrip(" .")
        if start_only:  # "stuck on his lip, then eaten completely" (video 170): only where it starts
            state = re.split(r",?\s+(?:and\s+)?then\b", state, maxsplit=1)[0].rstrip(" ,.")
        items.append(p["name"] + (f" ({look})" if look else "") + (f": {state}" if state else ""))
    out = []
    if items:
        out.append("Objects when the clip starts, each looking the same in every clip (only the action changes them): "
                   + "; ".join(items) + ".")
    if named:
        out.append("When they appear: " + "; ".join(f"{n} ({looks[n.lower()]})" for n in named) + ".")
    return _fit(" ".join(out), MAX_PROPS_LINE)


# An age in a saved appearance ("60 age", "aged 12", "in her 30s", "a 5-year-old"), dropped when the prompt gives one
AGE_WORDS = re.compile(r"\b(?:aged?\s*\d{1,3}|\d{1,3}\s*(?:-\s*)?(?:years?(?:[\s-]old)?|yrs?|age)|"
                       r"in (?:his|her|their) (?:early |mid |late )?\d0s|an? \d{1,3}-year-old)\b[\s,;]*", re.I)


def merged_sheet(sheet: dict, bible: dict | None) -> dict:
    """A picked character as this video shows them: each age, look, outfit or voice the prompt gives (the bible's
    cast entry) replaces their saved one; whatever the prompt doesn't say stays as saved. A character with a picture
    keeps their look as pictured: only a voice is taken from the prompt. `from_prompt` names the prompt's own entry
    (only the fields it gives)."""
    name = (sheet.get("name") or "").lower()
    over = next((c for c in (bible or {}).get("cast") or [] if name and c["name"].lower() == name), None)
    if over and sheet.get("image_path"):  # their picture is the reference: the prompt never redesigns it, only voices it
        over = {"voice": over["voice"]} if over.get("voice") else None
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


# A look the story changes on screen, and the "Avoid" words that would stop it (video 169: "Avoid: clean-shaven"
# kept Raju's fake moustache on after the goat ate it)
TAKEN_OFF = r"(?:eat|eats|ate|eaten|chew\w*|pull\w*|rip\w*|tear\w*|tore|torn|shav\w*|cut\w*|snatch\w*|knock\w*|" \
            r"blow\w*|blew|fall\w*|fell|remov\w*|tak\w+|took|gone|off|bare|missing)"
STORY_CHANGES = [
    (re.compile(rf"\b(?:moustache|mustache|beard)\b[^.]{{0,60}}\b{TAKEN_OFF}\b|\b{TAKEN_OFF}\b[^.]{{0,60}}\b"
                r"(?:moustache|mustache|beard)\b|\bbare (?:upper )?lip\b", re.I),
     re.compile(r"clean[- ]?shaven|beardless|shaven|no (?:moustache|mustache|beard)|without a (?:moustache|mustache|beard)",
                re.I)),
    (re.compile(rf"\b(?:turban|pagg|hat|cap|topi)\b[^.]{{0,60}}\b{TAKEN_OFF}\b|\b{TAKEN_OFF}\b[^.]{{0,60}}\b"
                r"(?:turban|pagg|hat|cap|topi)\b", re.I),
     re.compile(r"bare[- ]?headed|no (?:turban|hat|cap)|without a (?:turban|hat|cap)", re.I)),
]


def _story_changes(items: list[str], action: str) -> list[str]:
    """The "Avoid" items without those the action itself brings about (a moustache eaten off: not "clean-shaven")."""
    stops = [avoid for change, avoid in STORY_CHANGES if change.search(action or "")]
    if not stops:
        return items
    return [", ".join(x for x in re.split(r"\s*,\s*", item or "") if not any(s.search(x) for s in stops))
            for item in items]


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
    """`text` cut to at most `room` characters: after its last whole sentence when that keeps at least half of the
    room (a half sentence, "placing a glass on the", leaves Veo guessing), else at a word."""
    if len(text) <= room:
        return text
    cut = text[:max(room, 0)]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind(".\n"))
    if end >= room // 2:
        return cut[:end + 1]
    return cut[:cut.rfind(" ")].rstrip(" ,;:") if " " in cut else cut


def compile_prompt(scene: dict, cast: list[dict], style: str = "", language: str = "", aspect: str = "",
                   fit: bool = False, bible: dict | None = None, genre: str = "", music: bool = True,
                   room: int = MAX_PROMPT, flow: bool = False) -> str:
    """The clip prompt for one scene: format, style and palette, setting, camera, who is where (their reference
    picture and locked traits, else their library or bible words), action, lines in each character's voice, sound,
    what to avoid, then the quality rules. A scene that is only an action (and no style) keeps its words as written.
    With `fit`, the whole prompt is kept within `room`: the app's own notes and rules are said in fewer words first
    (the turning note, the "Avoid" line, the speaker and quality rules), and only then is the action shortened.
    `bible`: the video's story bible; `genre`: its type (app/genres.py); `music`: allow a soundtrack in the clip.
    `room`: the length `fit` keeps it within (prompt_limit, less when lines are added when it's sent).
    `flow` (a Flow video): it closes with the short flow_rules, and a music video's lines are sung."""
    by_id = {c["id"]: c for c in cast}
    bible = bible or {}
    invented = {c["name"].lower(): c for c in bible.get("characters") or []}
    style = look_only(style or bible.get("style") or "")
    plain = not is_structured(scene) and not style
    head, tail, avoid = [], [], clip_avoid(genre)
    if aspect in FORMATS:
        head.append(FORMATS[aspect])
    if style:
        head.append(_sentence(style))
    portraits = any(by_id.get(p.get("id"), {}).get("image_path") for p in scene.get("characters") or [])
    if bible.get("palette") and not (flow and portraits):  # on Flow the pictures set the colours (Ammi's sari)
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
        sheet = merged_sheet(by_id[p["id"]], bible) if p.get("id") in by_id \
            else invented.get(p["name"].lower()) or {}
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
            if flow and ANIMAL.search(" ".join([p["name"], sheet.get("appearance") or "", told.get("look") or ""])):
                # an animal (video 159's bunny): "same person, hair, outfit" made it stand and dress like a child
                people.append(_sentence(f"{p['name']}{where}: the same animal as reference image {pictured}, same "
                                        f"face, fur, colours and size{said}, from every side{locked}"))
            elif turns and flow:  # turning keeps the picture's face and hair: Veo invented a new one from behind
                people.append(_sentence(f"{p['name']}{where}: same person, {same} as reference image {pictured}"
                                        f"{said}; the picture shows them from the front, so from the side or back "
                                        f"keep that same face, hair and outfit{locked}"))
            elif turns:  # the picture fixes who they are, not which way they face
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
    if props_line(scene, bible, start_only=flow):
        head.append(props_line(scene, bible, start_only=flow))
    action = scene.get("action") or ""
    shorter = []  # (tail index, the same in fewer words), tried in this order before the action is cut
    if turns and flow:
        tail.append(TURN_NOTE_SHORT)
    elif turns:
        shorter.append((len(tail), TURN_NOTE_SHORT))
        tail.append(TURN_NOTE)
    lang = f" in {language}" if language else ""
    verb = "sings" if flow and genre == "music" else "says"
    sides = {p["name"].lower(): p.get("position") or "" for p in scene.get("characters") or []}
    placed = set()
    if flow:  # each line said where it happens in the action, by the person on their side: in time and in sync
        action, placed = speak_inline(action, scene.get("dialogue") or [], sides, lang)
    voices = []
    for i, d in enumerate(scene.get("dialogue") or []):
        voice = merged_sheet(by_id[d["id"]], bible).get("voice") if d.get("id") in by_id \
            else (invented.get(d["name"].lower()) or {}).get("voice") or ""
        side = sides.get(d["name"].lower()) if flow else ""
        if i in placed:
            entry = d["name"] + (f" ({side})" if side else "") + (f": {voice.rstrip(' .')}" if voice else "")
            if voice and entry not in voices:
                voices.append(entry)
            continue
        who = d["name"] + (f" ({side}; voice: {voice})" if side and voice else f" ({side or 'voice: ' + voice})"
                           if side or voice else "")
        tail.append(f"{who} {verb}{lang}: '{d['line']}'")
    if voices:
        tail.insert(0, "Voices: " + "; ".join(voices) + ".")
    if scene.get("sound"):
        tail.append(f"Sound: {_sentence(scene['sound'])}")
    rules = quality_rules(scene, style, music)
    if scene.get("dialogue") and not flow:
        speakers = {d["name"] for d in scene["dialogue"]}
        no_subs = "" if NO_TEXT in rules else " No subtitles."
        in_order = " " + SPEAKERS_IN_ORDER if len(scene["dialogue"]) > 1 and len(speakers) > 1 else ""
        shorter.append((len(tail), SPEAKERS_SHORT + no_subs + in_order + " " + LIP_SYNC))
        tail.append("Only the named characters speak, clearly and in sync with their lips." + no_subs + " "
                    + SPEAKERS + in_order + " " + LIP_SYNC)
    if NO_TEXT in rules and SIGNAGE_PLACES.search(" ".join([scene.get("setting") or "", action])):
        avoid.append(NO_TEXT_AVOID)
    if flow:  # what the story itself changes is never something to avoid
        avoid = _story_changes(avoid, " ".join([scene.get("action") or "", *(p.get("state") or "" for p in
                                                                             scene.get("props") or [])]))
    if _avoid(avoid):
        shorter.insert(min(1, len(shorter)), (len(tail), ""))
        tail.append(_avoid(avoid))
    ruled = None
    if flow:  # a few short lines, never shortened: the action gets the room
        tail += flow_rules(scene, style, music)
    else:
        shorter.append((len(tail), quality_rules(scene, style, music, short=True)))
        ruled = len(tail)
        tail.append(rules)
        # how it should look and perform: in every clip, never shortened
        tail.append(" ".join(([ACTING] if scene.get("characters") or scene.get("dialogue") else [])
                             + [QUALITY_ANIMATED if animated(scene, style) else QUALITY_FILM]))
    if action and fit:
        action = action if plain else _sentence(action)
        for i, words in shorter:
            if len("\n".join(head + [action] + tail)) <= room:
                break
            tail[i] = words
        tail = [x for x in tail if x]
        action = _fit(action, room - len("\n".join(head + tail)) - 1)
    if action:
        head.append(action if plain or fit else _sentence(action))
    if ruled is not None and not fit and len("\n".join(head + tail)) > room:  # an unfitted one may still fit
        tail[ruled] = quality_rules(scene, style, music, cinematic=False)
    return "\n".join(head + tail)


def still_prompt(scene: dict, cast: list[dict], style: str = "", bible: dict | None = None) -> str:
    """What a scene's locked first frame shows: the look, the place, the camera, who is where (the pictured ones by
    their picture's number, as scene_images orders them) and the first moment of the action. Everyone is described
    as the clip prompt describes them (compile_prompt): what the prompt says about a person (age, look, outfit) wins
    over their saved character and picture, so the frame and the clip agree and nothing changes mid-clip."""
    by_id, bible = {c["id"]: c for c in cast}, bible or {}
    invented = {c["name"].lower(): c for c in bible.get("characters") or []}
    lines = [_sentence(x) for x in (look_only(style or bible.get("style") or ""),
                                    f"Colour palette: {bible['palette']}" if bible.get("palette") else "") if x]
    place = ", ".join(x.rstrip(" .,;") for x in (scene.get("setting"), scene.get("time_of_day")) if x)
    if place:
        lines.append(_sentence(place))
    framing = (scene.get("shot") or {}).get("framing")
    if framing:
        lines.append(_sentence(f"{framing} shot" if "shot" not in framing else framing))
    people, pictured = [], 0
    for p in scene.get("characters") or []:
        sheet = merged_sheet(by_id[p["id"]], bible) if p.get("id") in by_id \
            else invented.get(p["name"].lower()) or {}
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
    if props_line(scene, bible):
        lines.append(props_line(scene, bible))
    first = re.split(r"(?<=[.!?])\s", (scene.get("action") or "").strip(), maxsplit=1)[0]
    if first:
        lines.append(_sentence(f"The first moment: {first}"))
    return _fit("\n".join(lines), MAX_PROMPT - 600)  # room for the worker's own lines


def line_words(text: str) -> int:
    """How many words a line is, for timing: words between spaces, and in a script written without spaces (Chinese,
    Japanese, Thai…) every CHARS_PER_WORD characters, so a whole sentence there isn't one word."""
    n = 0
    for token in (text or "").split():
        chars = len(NO_SPACE_SCRIPT.findall(token))
        n += -(-chars // CHARS_PER_WORD) if chars else 1
    return n


def spoken_words(scene: dict) -> int:
    return sum(line_words(d["line"]) for d in scene.get("dialogue") or [])


def speaker_count(scene: dict) -> int:
    return len({d["name"].lower() for d in scene.get("dialogue") or [] if d["line"].strip()})


def keep_places(script: list[dict]) -> list[dict]:
    """The script with everyone who was in the scene before standing where they stood there (Veo, carrying on from
    that scene's last frame, tells people apart by where they stand: a swap gives each the other's lines). A newcomer
    keeps the place the script gives them. Where the action names the place it moved them to ("Shani stands on frame
    left"), it says the kept one instead."""
    out, before = [], {}
    for sc in script:
        people, action = [], sc.get("action") or ""
        for p in sc.get("characters") or []:
            was, now = before.get(p["name"].lower()), p.get("position") or ""
            if was and now and was != now:
                action = re.sub(rf"((?<!\w){re.escape(p['name'])}(?!\w)[^.]{{0,40}}?){re.escape(now)}",
                                lambda m: m.group(1) + was, action, flags=re.I)
            people.append({**p, "position": was or now})
        before = {p["name"].lower(): p["position"] for p in people if p["position"]}
        out.append({**sc, "characters": people, "action": action})
    return out


def holds_and_claps(scene: dict) -> bool:
    """True when someone is told to hold something (in the action, or a prop's state when the scene starts) and also
    to clap, which takes both hands."""
    held = " ".join([scene.get("action") or "", *((p.get("state") or "") for p in scene.get("props") or [])])
    return bool(HOLDING.search(held) and CLAPPING.search(scene.get("action") or ""))


def busy_hands(scene: dict) -> bool:
    """True when someone holds something in both hands and is also told to point, wave or gesture."""
    action = scene.get("action") or ""
    return bool(BOTH_HANDS.search(action) and GESTURE.search(action))


def words_per_clip(seconds: int = 10, speakers: int = 1) -> int:
    """About how many spoken words fit in a `seconds`-long clip, said clearly with room to pause: 25 in Gemini's 10 s;
    in Flow's 8 s, 16 with one speaker and 14 with two or more."""
    if seconds == FLOW_SECONDS:
        return FLOW_WORDS["more" if speakers > 1 else "one"]
    return round(MAX_SPOKEN_WORDS * seconds / 10)


def scene_warnings(scene: dict, prompt: str, cast: list[dict] = (), seconds: int = 10,
                   limit: int = MAX_PROMPT) -> list[str]:
    """What may go wrong with a scene's clip. `limit`: how long a clip prompt may be (prompt_limit)."""
    out = []
    if busy_hands(scene):
        out.append("Someone holds something in both hands and also points, waves or gestures: the video may grow "
                   "a third hand. Say which hand does what (e.g. 'holds the roti in her left hand, points with her "
                   "right').")
    if seconds == FLOW_SECONDS and holds_and_claps(scene):
        out.append("Someone holds something and also claps, which takes both hands: the video may leave it standing "
                   "or floating on its own. Have them put it down first, or tap it to the beat instead of clapping.")
    words, speakers = spoken_words(scene), speaker_count(scene)
    fit = words_per_clip(seconds, speakers)
    if words > fit:
        talking = " with two or more people talking" if speakers > 1 and fit != words_per_clip(seconds) else ""
        out.append(f"{words} spoken words: about {fit} fit in {seconds} seconds{talking}, so lines may be rushed or "
                   "cut short. Move a line to the next scene.")
    if len(prompt) > limit:
        out.append(f"The clip prompt is {len(prompt):,} characters and {limit:,} can be sent, so it's shortened when "
                   "it's sent: the app's own rules are said in fewer words, and if that isn't enough the end of the "
                   "action is cut. Shorten the action or setting a little to keep all of it.")
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
