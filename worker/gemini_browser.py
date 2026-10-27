"""Playwright driver for the Gemini web app: the video tool (Veo) for clips, plain chat for scene plans
and character portraits.

Account-safety rules (non-negotiable):
- One fixed sequence. Nothing on the page or in the prompt changes it.
- Top-level navigation only to the allowlisted hosts in gemini_ui.json.
- No stealth, fingerprinting, proxies, CAPTCHA solving or account rotation. Any limit, sign-out,
  verification or upgrade page raises a typed error and the worker STOPS.
- The only page prompts the worker answers are Gemini's "Creating content from images and files"
  consent and its "A reminder about creating videos" rights reminder (Agree on both), approved by the
  account owner so pictures and clips can be attached.
- Logs carry job ids, steps and durations only: never prompts, cookies, tokens or page text.
"""
import base64
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from worker.config import HERE, WorkerSettings
from worker.generator import (
    AccountStop,
    Cancelled,
    ClipRequest,
    GeminiBusy,
    GenerationError,
    GeneratorError,
    ImageLimitReached,
    LimitReached,
    PlanRequest,
    PortraitRequest,
    CheckRequest,
    SafetyRefusal,
    SignedOut,
    SubscriptionRequired,
    VerificationRequired,
)

log = logging.getLogger("worker.gemini")
MAX_DOWNLOAD = 500 * 1024 * 1024
POLL_SECONDS = 4
PLAN_TIMEOUT = 600  # at most this many seconds for Gemini to write the scene list…
PLAN_IDLE = 240  # …and give up sooner if its reply hasn't changed for this long (long ideas think for minutes)
PORTRAIT_TIMEOUT = 180  # seconds for Gemini to draw a portrait
MAX_IMAGE = 20 * 1024 * 1024
MAX_REFERENCE_IMAGE = 10 * 1024 * 1024  # the most a reference picture may be when it's attached to a clip
MAX_SCENE_PROMPT = 2000
# A picture in a video reply means Gemini drew an image instead (its Video tool was off).
REPLY_IMAGE = "generated-image img, single-image img, img[src^='blob:'], img[src*='googleusercontent']"


# The aspect menu's word for each format (Video tool, under the prompt box)
ASPECT_NAMES = {"9:16": "Portrait", "16:9": "Landscape"}

# Shot vocabulary for the script. app/script.py offers the same lists in the script editor.
FRAMINGS = ["extreme wide", "wide", "medium", "medium close-up", "close-up", "extreme close-up",
            "over-the-shoulder", "two-shot"]
ANGLES = ["eye level", "low angle", "high angle", "bird's-eye", "dutch angle"]
MOVEMENTS = ["static", "slow push-in", "pull-out", "pan left", "pan right", "tilt up", "tilt down", "tracking",
             "dolly", "handheld", "orbit", "crane"]
LENSES = ["24mm wide-angle lens", "35mm lens", "50mm lens", "85mm lens with shallow depth of field"]
POSITIONS = ["frame left", "center", "frame right", "foreground", "background"]
MAX_SPOKEN_WORDS = 25  # per 10 seconds (as app/script.py): a shorter clip gets proportionally fewer
MAX_SCENE_IMAGES, MAX_IN_SHOT = 3, 6  # as app/script.py: pictures one clip takes, people one shot may hold
_BIBLE_EXAMPLE = ('{"title": "...", "style": "...", "palette": "...", "setting": "...", "motif": "...", '
                  '"characters": [{"name": "...", "look": "...", "never": "..."}], '
                  '"cast": [{"name": "...", "age": "...", "look": "...", "outfit": "...", "voice": "..."}]}')
_EXAMPLE = ('{"setting": "...", "time_of_day": "...", "characters": [{"name": "...", "position": "..."}], '
            '"shot": {"framing": "...", "angle": "...", "movement": "...", "lens": "..."}, "action": "...", '
            '"dialogue": [{"name": "...", "line": "..."}], "sound": "..."}')


TEXT_ONLY = ("TEXT ONLY: this is a writing task. Do not create, generate or start any video, image, audio or other "
             "media, and do not use any tool. Reply only with the JSON text asked for below.")


def plan_instruction(req: PlanRequest) -> str:
    total = req.scene_count * req.clip_seconds
    if req.cast:
        sheets = "\n".join(
            f"- {c['name']}: " + "; ".join(x for x in (c.get("appearance"), c.get("outfit")) if x)
            + (f". Voice: {c['voice']}" if c.get("voice") else "")
            + (f". Personality: {c['personality']}" if c.get("personality") else "")
            for c in req.cast)
        cast = ("Cast (use these names exactly; any other character the idea names, such as a friend or a baby, "
                "you invent and keep; the cast's saved looks, outfits and voices are added to every scene for you, so "
                "don't describe them again, except that where the idea itself says a cast member's age, look, "
                "clothes or voice, the idea wins: copy those words into the bible's cast entry for them, and leave "
                "out what the idea doesn't say). Give each cast member "
                "the role in the idea that fits their age, gender and look, and name them in the action of every "
                "scene they are in; when the idea says 'friends' or 'a group', these are the people. Leave out a "
                "cast member the idea has no fitting role for, and never use one as a different person (a "
                "12-year-old is not the baby, a young woman is not the grandmother): invent that person instead, "
                "with their look in the bible:\n" + sheets + "\n")
    else:
        cast = ("Invent the characters the idea needs and give each a short name. Each scene is made on its own, "
                "so fix each character's look once in the bible: it is added to every scene they are in for you, so "
                "list them in that scene's characters by the same name.\n")
    style = (f"Visual style for every scene: {req.style}\n" if req.style else
             "If the idea names a visual style (3D cartoon, anime, photorealistic...), begin every scene's setting "
             "with that style in a few words: the generator sees nothing but the scene.\n")
    # 3 per shot keeps faces steady (and is all the pictures one clip takes); more only so the whole cast fits
    in_shot = min(max(MAX_SCENE_IMAGES, -(-len(req.cast or []) // max(req.scene_count, 1))), MAX_IN_SHOT)
    spread = (f" Spread the cast across the scenes so each scene has at most {in_shot}."
              if len(req.cast or []) > MAX_SCENE_IMAGES else "")
    language = f"All spoken lines are in {req.language}.\n" if req.language else ""
    rules = f"Rules for this kind of video:\n{req.rules}\n" if req.rules else ""
    return (
        # Gemini picks a tool from the request: an idea full of 'video', '9:16' and 'lip-sync' can start Veo (or
        # hang while it tries), so say first that this is writing only, and fence the idea off below.
        f"{TEXT_ONLY}\n\n"
        f"You are a film director writing the shooting script for an AI video generator that makes one "
        f"{req.clip_seconds}-second clip per scene. Turn the idea below into exactly {req.scene_count} consecutive "
        f"scenes that together play as one continuous {total}-second {req.aspect} video.\n"
        + cast + style + language + rules +
        "Rules:\n"
        "- The generator never sees the other scenes, so give every scene its full setting and keep places, "
        "props and lighting consistent from scene to scene.\n"
        f"- Describe only what happens in that scene's {req.clip_seconds} seconds and keep the story moving: one "
        "main action per scene at a natural speed; split anything longer across scenes.\n"
        "- Scene by scene, the video's seconds are: "
        + ", ".join(f"scene {i + 1} = {i * req.clip_seconds}-{(i + 1) * req.clip_seconds} s"
                    for i in range(req.scene_count)) +
        ". If the idea has a timeline ('0-4 seconds: ...'), put each timed moment in the scene whose seconds it "
        "falls in and keep every moment, shortened if needed, rather than dropping any.\n"
        "- Never drop anything the idea writes out: every scene, character, action and line of dialogue in it "
        f"must be in the script. When the idea has more scenes than these {req.scene_count}, put neighbouring ones "
        f"together in one scene, in order (two short moments can share a {req.clip_seconds}-second scene). Keep "
        "written lines word "
        "for word, in their language and script.\n"
        "- In every scene's action, say where each person is in relation to the main place or object and what their "
        "role there is, so nobody swaps places or roles (e.g. 'the seller stands behind the counter; Chacha stands in "
        "front of it as the customer'). Keep those places from scene to scene unless the story moves them.\n"
        "- Each line of dialogue belongs to the person who says it in the idea: put it under exactly that name, never "
        "give one person's line to another, and never merge two people's lines into one.\n"
        f"- At most {in_shot} people or animals in a shot.{spread} Give any group an exact count and keep it in every scene ('three "
        "friends', never 'a group of friends'), and describe extras briefly and in the same words each time.\n"
        "- In each scene's characters, list everyone who is seen in it, the ones you invented too, by the same "
        "name every time.\n"
        "- Each scene starts where the previous one ended: same place, positions, props, lighting and time of day, "
        "unless the story moves on.\n"
        "- Scene 1 hooks the viewer in its first 2 seconds and the last scene pays the idea off. End each scene's "
        "action on a moment that leads into the next (a look, a sound, something arriving), so the cuts feel "
        "connected.\n"
        "- One camera movement per shot, and keep the subject fully in frame while it moves.\n"
        "- Everything keeps its state from moment to moment and scene to scene until the action itself changes it: "
        "every object (where it is, who holds it and in which hand, whether it's full, broken, open, lit or eaten), "
        "every person's clothes and accessories (worn, removed, torn, wet, dirty), their hair, make-up, marks and "
        "injuries, where each person stands and which way they face, and the place itself (doors and windows, "
        "lights, weather, time of day, anything spilled, moved or knocked over). When something changes, show the "
        "moment it changes. At the start of every scene, say the state of everything that matters in it.\n"
        "- When one scene has to hold several of the idea's moments, keep each one small and slow. At most one fast "
        "movement (a run, a chase, a jump) per scene, at its end, and never in the same scene as a long line. A "
        "fast move gets the whole scene if it's the point of the scene.\n"
        "- Age: when the idea calls someone a man or a woman (even a 'young man'), they are an adult and must look "
        "like one, or the generator may take them for a child and refuse any scene where they are shouted at, "
        "chased or in love. In their bible look and every scene's action give an age of late 20s or older and at "
        "least one clearly grown-up feature: for a man a mustache, stubble or beard (unless the idea says "
        "clean-shaven), for a woman a mature face; adult height and proportions. Never baby-faced, never "
        "child-sized, and never list a beard, mustache or stubble under 'never' for an adult man.\n"
        "- People have two hands: when someone holds something in both hands they can't also point, wave or gesture. "
        "Say which hand does what ('holds the roti in her left hand and points with her right').\n"
        "- Avoid what the generator does badly: crowds in close-up, fast tangled body contact (fights, hugs while "
        "spinning), small detailed hand work, mirrors, and written text unless the idea needs it.\n"
        "- Direct it like a film: open with an establishing wide shot, use medium shots for action and close-ups "
        "for emotion and dialogue, and keep screen direction consistent (whoever is frame left in a conversation "
        "stays frame left).\n"
        "- For spins, twirls and dance turns: at most one slow full turn per scene, written out step by step "
        "(faces camera, turns to profile, back to camera, profile, faces camera again). Use a medium or wide "
        "framing, never a close-up, while someone turns. If the idea only needs the view to circle a person, keep "
        "the person facing one way and use movement 'orbit' instead of making them spin.\n"
        f"- framing is one of: {', '.join(FRAMINGS)}. angle is one of: {', '.join(ANGLES)}. movement is one of: "
        f"{', '.join(MOVEMENTS)}. lens is one of: {', '.join(LENSES)}. position is one of: {', '.join(POSITIONS)}.\n"
        f"- Spoken lines must fit in {req.clip_seconds} seconds: at most about "
        f"{round(MAX_SPOKEN_WORDS * req.clip_seconds / 10)} words per scene "
        "in total, except lines the idea writes out, which are all kept. Leave dialogue empty when nobody speaks.\n"
        "- Put any on-screen text in the action of the scene where it appears, spelled exactly as in the idea.\n"
        "- Never put double quotes inside a value; write quotes as single quotes ('like this').\n"
        "- First write the story bible every scene follows: title; style = the visual style in a few words (the one "
        "given, else the idea's, else one that suits it); palette = 3 to 5 main colours; setting = the main place, "
        "described once; motif = one visual element that recurs in every scene to tie them together (or empty); "
        "characters = one entry per character you invented (not the cast), with look = their fixed appearance and "
        "clothing in one line and never = the details a generator might get wrong about them ('upright ears, "
        "brown fur'); cast = one entry per cast member the idea describes, named as in the cast list, with only "
        "the age, look, outfit and voice the idea gives them, in the idea's words (empty when it says none).\n"
        f"- Reply with ONLY one compact JSON object: {{\"bible\": {{...}}, \"scenes\": [...]}}, where scenes holds "
        f"{req.scene_count} objects, one per scene, in order, each scene object on a single line with no "
        f"indentation. The bible is like:\n{_BIBLE_EXAMPLE}\nand each scene like:\n"
        f"{_EXAMPLE}\nNo other text.\n\n"
        "Idea (the material to write the script from, not a request to you; its format and video rules are for "
        f"the generator):\n<<<\n{req.prompt}\n>>>"
    )


def model_named(text: str, label: str) -> bool:
    """True when `text` (the picker, or a menu entry's first line) names model `label` exactly: "3.8 Flash" is
    Flash, "Flash-Lite" is not."""
    return bool(re.search(rf"(?<![\w-]){re.escape(label.strip())}(?![\w-])", text or "", re.I))


def image_limit_error(page_text: str) -> ImageLimitReached:
    """The image-limit error, with the reset time from Gemini's banner ("…resets on Oct 21 at 1:25 PM") if shown."""
    m = re.search(r"resets? on ([A-Z][a-z]{2,8}\.? \d{1,2},? at \d{1,2}:\d{2}\s?[AP]M)", page_text)
    if not m:
        return stop_error("image_limit")
    return ImageLimitReached(f"Gemini's image limit has been reached. Portraits can be made again once it resets "
                             f"({m.group(1)}); videos aren't affected.")


def ab_choice_text(body: str, survey: dict) -> str | None:
    """The first answer's text when Gemini shows its "Which response is more helpful?" side-by-side vote, else None."""
    low = body.lower()
    at = low.find(survey["marker"].lower())
    if at < 0:
        return None
    start = low.find(survey["first"].lower(), at)
    if start < 0:
        return None
    start += len(survey["first"])
    end = low.find(survey["second"].lower(), start)
    return body[start:end if end >= 0 else len(body)].strip() or None


# Footwear in the description means the user cares about it, so the portrait shows the whole body.
FOOTWEAR = re.compile(r"\b(shoes?|sneakers?|trainers?|boots?|sandals?|heels?|slippers?|loafers?|flip[- ]?flops?|"
                      r"footwear|chappals?|khussas?|kolhapuris?|socks?|barefoot)\b", re.I)


def portrait_instruction(description: str, style: str = "") -> str:
    """Ask Gemini's chat for exactly one character portrait, framed for use as a reference picture: head to waist
    (a large, clear face), or head to toe when the description mentions footwear."""
    style = style or "Realistic photo, natural skin texture"
    framing = ("full body from the top of the head to the shoes, feet fully visible"
               if FOOTWEAR.search(description) else "framed from the head to the waist")
    return (
        "Generate one image: a character reference portrait for a film.\n"
        f"- Vertical 3:4 image, {framing}, the character facing the camera.\n"
        "- Plain neutral studio background, soft even lighting, sharp focus on the face.\n"
        "- Only this one person, no text, no captions, no watermark.\n"
        f"- Style: {style}\n"
        "Draw exactly this character, keeping every detail:\n"
        f"{description}"
    )


def still_instruction(description: str, aspect: str, names: list[str], previous: bool) -> str:
    """Ask Gemini's chat for one scene's first frame, drawn from the attached pictures: the characters (in `names`
    order), then the previous shot's last frame when `previous`."""
    shape = "Vertical 9:16" if aspect == "9:16" else "Widescreen 16:9"
    who = "; ".join(f"picture {i} is {n}" for i, n in enumerate(names, 1))
    refs = (f"- The attached pictures are the exact characters ({who}): the same faces, hair, clothes, colours and "
            "style, redrawn in the scene, not pasted. Where the shot below says what someone wears or looks like, "
            "that wins over their picture.\n" if names else "")
    if previous:
        refs += (f"- Picture {len(names) + 1} is the last frame of the previous shot: keep its place, light and "
                 "everyone's clothes and colours.\n")
    return (
        "Generate one image: the opening frame of a video shot, to start the video from.\n"
        f"- {shape} image, a single frame, no text, no captions, no watermark, no borders.\n"
        + refs +
        f"The shot:\n{description}"
    )


CHECK_KEYS = ("same_people", "same_clothes", "same_place", "normal_bodies", "story_happened", "right_speakers",
              "lips_in_sync", "same_voices")


def check_instruction(names: list[str], previous: bool, action: str = "", lines: list[dict] | None = None,
                      outfits: dict[str, str] | None = None, stills: int = 0) -> str:
    """Ask Gemini's chat (text only) to watch a new clip, with its sound, and check it: the people against their
    pictures (named after them), the place and voices against the previous clip, and the action and lines against
    what the scene should show and say. `outfits`: clothes the video asks for instead of the picture's."""
    lines, outfits = lines or [], {n: o for n, o in (outfits or {}).items() if n in names and o}
    out = [
        "TEXT ONLY: this is a checking task. Do not create, generate or edit any image or video, and do not use "
        "any tool. Reply only with the JSON asked for below.",
        "Watch and listen to the attached video new-shot.mp4."
        + (" previous-shot.mp4 is the shot just before it." if previous else ""),
    ]
    if names:
        out.append("The attached pictures are the characters, each named after them: "
                   + ", ".join(f"{n}" for n in names) + ".")
    if action:
        out.append(f"What new-shot.mp4 should show: {action}")
    if lines:
        out.append("What should be said, in order: " + " ".join(
            f"{x['name']}{' (' + x['position'] + ')' if x.get('position') else ''} says: '{x['line']}'." for x in lines))
    else:
        out.append("Nobody should speak in it.")
    instead = "; ".join(f"{n} should wear {o} instead of the clothes in their picture" for n, o in outfits.items())
    checks = ["same_people = every pictured character who appears is the same person (face, hair, age, body) as "
              "their picture", "same_clothes = dressed the same as their picture" + (f" ({instead})" if instead else "")]
    if previous:
        checks.append("same_place = it carries on from the end of previous-shot.mp4: the same place and light, "
                      "everyone in the same clothes and spot, and every object in the same state and place, unless "
                      "the action changes it")
    body = (f"normal_bodies = in each still new-shot-frame-1 to new-shot-frame-{stills} (taken from the video), count "
            "every hand you can see and say whose arm it belongs to; false if any person has more than two arms or "
            "hands, or a hand belongs to nobody" if stills else
            "normal_bodies = everyone has two arms, two hands, five fingers, nothing extra or missing")
    checks += [body,
               "story_happened = what it should show really happens, in order"]
    if lines:
        checks += ["right_speakers = each line is said by the right person and nobody else talks",
                   "lips_in_sync = the speaker's lips move with the words"]
    else:
        checks.append("right_speakers = nobody talks")
    if previous:
        checks.append("same_voices = each character who speaks sounds like the same voice and accent as in "
                      "previous-shot.mp4 (true when they don't speak in both)")
    out.append("Check new-shot.mp4: " + "; ".join(checks) + ". Ignore pose, expression, camera angle, small details "
               "and the exact wording. Reply with ONLY a compact JSON object with these keys as true or false, plus "
               '"problems": what is wrong in a few words (empty when nothing is), like {"same_people": true, '
               '"story_happened": false, "problems": "she never hides the phone"}. No other text.')
    return "\n".join(out)


def parse_check(text: str) -> dict:
    """{"ok", "problems"} from Gemini's check reply; same_place counts only when it's given."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise GenerationError("Gemini didn't reply to the clip check.")
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        raise GenerationError("Gemini's clip check wasn't valid JSON.") from None
    if not isinstance(obj, dict) or not isinstance(obj.get("same_people"), bool):
        raise GenerationError("Gemini's clip check didn't say whether the people match.")
    failed = [k for k in CHECK_KEYS if obj.get(k) is False]
    return {"ok": not failed, "problems": str(obj.get("problems") or "")[:300], "failed": failed}


def image_kind(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def parse_plan(text: str, count: int) -> dict:
    """{"bible", "scenes"} from Gemini's reply: a JSON object with both (possibly inside a code block), or just
    the list of scenes (see parse_script), which then has an empty bible."""
    start, arr = text.find("{"), text.find("[")
    if start < 0 or 0 <= arr < start:
        return {"bible": {}, "scenes": parse_script(text, count)}
    body = text[start:text.rfind("}") + 1]
    try:
        obj = json.loads(body)
    except ValueError:
        try:
            obj = json.loads(_escape_stray_quotes(body))
        except ValueError:
            obj = None
    if not isinstance(obj, dict) or not isinstance(obj.get("scenes"), list):
        if obj is None and arr >= 0:  # a broken object: the scene list on its own may still read
            return {"bible": {}, "scenes": parse_script(text[arr:], count)}
        raise GenerationError("Gemini's script wasn't valid JSON." if obj is None
                              else "Gemini didn't reply with a list of scenes.")
    scenes = [sc for sc in map(_scene_item, obj["scenes"]) if sc]
    if len(scenes) < count:
        raise GenerationError(f"Gemini wrote {len(scenes)} scene(s) instead of {count}.")
    bible = obj.get("bible") if isinstance(obj.get("bible"), dict) else {}
    return {"bible": bible, "scenes": scenes[:count]}


def parse_script(text: str, count: int) -> list[dict]:
    """The script scenes from Gemini's reply (a JSON array of objects, possibly inside a code block). A reply
    that is a list of plain prompts still works: each one becomes a scene's action."""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise GenerationError("Gemini didn't reply with a list of scenes.")
    body = text[start:end + 1]
    try:
        items = json.loads(body)
    except ValueError:
        try:  # a list of objects: repair quotes inside values; a list of strings: split it by lines
            items = json.loads(_escape_stray_quotes(body)) if body[1:].lstrip().startswith("{") \
                else _loose_scene_list(body[1:-1])
        except ValueError:
            items = None
        if not items:
            raise GenerationError("Gemini's list of scenes wasn't valid JSON.") from None
    scenes = [sc for sc in map(_scene_item, items) if sc] if isinstance(items, list) else []
    if len(scenes) < count:
        raise GenerationError(f"Gemini wrote {len(scenes)} scene(s) instead of {count}.")
    return scenes[:count]


def _scene_item(item) -> dict | None:
    if isinstance(item, str):
        return {"action": item.strip()[:MAX_SCENE_PROMPT]} if item.strip() else None
    if isinstance(item, dict) and any(item.values()):
        return item
    return None


def _escape_stray_quotes(body: str) -> str:
    """Escape double quotes inside JSON strings (Gemini quoting dialogue): a quote that closes a string is
    followed by `:`, `,`, `}` or `]`; any other one inside a string is part of the text."""
    out, in_str, i = [], False, 0
    while i < len(body):
        ch = body[i]
        if in_str and ch == "\\":
            out.append(body[i:i + 2])
            i += 2
            continue
        if ch == '"':
            if not in_str:
                in_str = True
            elif body[i + 1:].lstrip()[:1] in (":", ",", "}", "]", ""):
                in_str = False
            else:
                ch = '\\"'
        out.append(ch)
        i += 1
    return "".join(out)


def _loose_scene_list(body: str) -> list[str]:
    """The items of a one-scene-per-line array whose strings hold unescaped quotes (Gemini quoting dialogue)."""
    body = body.strip().rstrip(",").strip()
    if len(body) < 2 or body[0] != '"' or body[-1] != '"':
        return []
    return [s.replace('\\"', '"') for s in re.split(r'"\s*,\s*\n\s*"', body[1:-1])]


# ---- UI config ---------------------------------------------------------------
def load_ui(path: Path | None = None) -> dict:
    return json.loads((path or HERE / "gemini_ui.json").read_text())


class StopText:
    """Classifies visible text into a stop kind using the regexes in gemini_ui.json."""

    ORDER = ("verification", "image_limit", "limit", "no_subscription", "real_people", "safety", "busy", "error")

    def __init__(self, ui: dict):
        self.patterns = {k: [re.compile(p, re.I | re.M) for p in v] for k, v in ui["patterns"].items()}

    def classify(self, text: str) -> str | None:
        for kind in self.ORDER:
            if any(p.search(text) for p in self.patterns.get(kind, [])):
                return kind
        return None

    def signed_out(self, text: str) -> bool:
        return any(p.search(text) for p in self.patterns.get("signed_out", []))

    def misrouted(self, text: str) -> bool:
        """A chat reply (plan) that went to the Video tool: "I'm generating your video…"."""
        return any(p.search(text) for p in self.patterns.get("plan_misroute", []))


STOP_ERRORS: dict[str, Callable[[], GeneratorError]] = {
    "verification": lambda: VerificationRequired(
        "Google wants to confirm it's you (CAPTCHA or security check). Generation is paused: open the "
        "Gemini sign-in window in Settings, complete the check by hand, then resume."),
    "image_limit": lambda: ImageLimitReached(
        "Gemini's image limit has been reached. Portraits can be made again once it resets; videos aren't affected."),
    "limit": lambda: LimitReached(
        "Gemini's video limit has been reached. Generation is paused; resume in Settings once the limit resets."),
    "no_subscription": lambda: SubscriptionRequired(
        "This Google account can't make videos in Gemini (an upgrade is required). Generation is paused."),
    "real_people": lambda: SafetyRefusal(
        "Gemini won't make this clip because it takes the people in the reference pictures for real people. "
        "Real people's photos can't be used this way. If they're your own made-up characters, give them "
        "cartoon-style portraits (Characters → Generate portrait, e.g. Pixar-style 3D) and leave 'realistic' "
        "out of the style, then retry."),
    "safety": lambda: SafetyRefusal(
        "Gemini declined this clip for safety reasons (for example, it thought a character looks like a child in "
        "an unsafe or compromising moment). Change that moment or make the character clearly adult, then retry."),
    "busy": lambda: GeminiBusy(
        "Gemini said it's at full capacity right now (“unusually high traffic”). It's tried again after a few "
        "minutes on its own."),
    "error": lambda: GenerationError(
        "Gemini answered with its own error (“I encountered an error…”) instead of the clip. That comes from "
        "Gemini, not this app; it usually passes. Retry in a few minutes, or later if the daily video limit "
        "may be near."),
    "signed_out": lambda: SignedOut(
        "Gemini is signed out. Open the Gemini sign-in window in Settings, sign in, then resume."),
}


def stop_error(kind: str) -> GeneratorError:
    return STOP_ERRORS[kind]()


# ---- browser launch ------------------------------------------------------------
def _chrome_path() -> str:
    if sys.platform == "darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    return "google-chrome"


def launch_kwargs(settings: WorkerSettings, headless: bool | None = None) -> dict:
    args = ["--no-first-run", "--no-default-browser-check"]
    if sys.platform.startswith("linux"):
        args.append("--password-store=basic")  # same cookie encryption as the plain sign-in window
    kw = {
        "user_data_dir": str(settings.profile_dir),
        "headless": settings.BROWSER_HEADLESS if headless is None else headless,
        "args": args,
        # Playwright's mock keychain would encrypt cookies with a different key than the plain
        # sign-in window uses, so the automated launch couldn't read the human's session.
        "ignore_default_args": ["--use-mock-keychain"],
        # Keep Chrome's sandbox on (Playwright's default adds --no-sandbox, which Chrome warns about).
        "chromium_sandbox": True,
        "accept_downloads": True,
        "viewport": {"width": 1280, "height": 900},
    }
    if settings.BROWSER_CHANNEL:
        kw["channel"] = settings.BROWSER_CHANNEL
    return kw


PROFILE_IN_USE = ("The Gemini browser window is still open. Quit it (Chrome menu → Quit, or ⌘Q in that "
                  "window), then click Check session.")


def profile_browser_pid(settings: WorkerSettings) -> int | None:
    """PID of a Chrome still running on the profile (from its SingletonLock "host-pid" link), else None."""
    try:
        target = os.readlink(settings.profile_dir / "SingletonLock")
        pid = int(target.rsplit("-", 1)[1])
        os.kill(pid, 0)
    except (OSError, ValueError, IndexError):
        return None
    return pid


def launch_context(p, settings: WorkerSettings, wait: float = 20.0, attempts: int = 3,
                   headless: bool | None = None):
    """launch_persistent_context, but first wait for any other Chrome on the profile to exit.

    A second Chrome on the same profile hands over to the running one and quits at once, which
    Playwright reports as an opaque TargetClosedError. That also happens for a moment while a
    just-closed Chrome is still shutting down, so a closed-at-launch is retried after a pause."""
    for attempt in range(1, attempts + 1):
        deadline = time.monotonic() + wait
        while profile_browser_pid(settings) is not None:
            if time.monotonic() >= deadline:
                raise GenerationError(PROFILE_IN_USE)
            time.sleep(0.5)
        try:
            return p.chromium.launch_persistent_context(**launch_kwargs(settings, headless))
        except Exception as e:
            if type(e).__name__ != "TargetClosedError" or attempt == attempts:
                raise
            log.info("browser closed during launch (attempt %d); retrying", attempt)
            time.sleep(3.0)


def plain_browser_command(settings: WorkerSettings, ui: dict) -> list[str]:
    """A normal, non-automated browser on the same profile, for a human to sign in."""
    if settings.BROWSER_CHANNEL == "chrome":
        exe = _chrome_path()
    else:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            exe = p.chromium.executable_path
    cmd = [exe, f"--user-data-dir={settings.profile_dir}", "--no-first-run", "--no-default-browser-check"]
    if sys.platform.startswith("linux"):
        cmd.append("--password-store=basic")
    cmd.append(ui["app_url"])
    return cmd


def install_navigation_guard(context, allowed_hosts: list[str]) -> None:
    allowed = set(allowed_hosts)

    def guard(route, request):
        try:
            top = request.is_navigation_request() and request.frame.parent_frame is None
        except Exception:
            top = False
        if top:
            host = urlparse(request.url).hostname or ""
            if request.url.startswith("http") and host not in allowed:
                log.warning("blocked navigation to a non-allowlisted host")
                route.abort()
                return
        route.continue_()

    context.route("**/*", guard)


# ---- page helpers --------------------------------------------------------------
class Page:
    """Thin wrapper that resolves selector keys from gemini_ui.json."""

    def __init__(self, page, ui: dict):
        self.page = page
        self.ui = ui
        self.sel = ui["selectors"]

    def find(self, key: str, timeout: float = 5.0, **fmt):
        deadline = time.monotonic() + timeout
        while True:
            for s in self.sel.get(key, []):
                loc = self.page.locator(s.format(**fmt) if fmt else s)
                try:
                    for i in range(min(loc.count(), 5)):  # first *visible* match, not just the first
                        if loc.nth(i).is_visible():
                            return loc.nth(i)
                except Exception:
                    continue
            if time.monotonic() >= deadline:
                return None
            self.page.wait_for_timeout(400)

    def matches(self, key: str) -> str | None:
        """First candidate that matches at all (for diagnostics)."""
        for s in self.sel.get(key, []):
            try:
                if self.page.locator(s).count():
                    return s
            except Exception:
                continue
        return None

    def body_text(self) -> str:
        try:
            return self.page.inner_text("body", timeout=5000)
        except Exception:
            return ""

    def screenshot(self, settings: WorkerSettings, name: str) -> str | None:
        settings.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        path = settings.diagnostics_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}.png"
        try:
            self.page.screenshot(path=str(path))
            path.chmod(0o600)
            return str(path)
        except Exception:
            return None


def check_session(pg: Page, stop: StopText, prompt_timeout: float = 20.0) -> None:
    """Raise a typed AccountStop if the page isn't a signed-in, usable Gemini; else return."""
    url = pg.page.url
    host = urlparse(url).hostname or ""
    if host == "accounts.google.com":
        raise stop_error("signed_out")
    if "/sorry/" in url or pg.matches("captcha"):
        raise stop_error("verification")
    text = pg.body_text()
    if stop.classify(text) == "verification":
        raise stop_error("verification")
    box = pg.find("prompt_input", timeout=prompt_timeout)
    # Gemini lets signed-out visitors chat too, so require a positive signed-in signal (account avatar).
    signed_in = pg.find("account_button", timeout=3) is not None
    if not signed_in or pg.find("sign_in_button", timeout=0.5) is not None:
        raise stop_error("signed_out")
    if box is None:
        raise GenerationError("Couldn't find Gemini's prompt box; the page may have changed. "
                              "Run `python -m worker check-gemini`.")


# ---- shared by the Gemini and Flow drivers -------------------------------------
def run_in_browser(settings: WorkerSettings, ui: dict, jid: str, work, site: str = "Gemini",
                   where: Callable[[], str] | None = None):
    """Launch the profile, run `work(pg, ctx)`, always close the browser. `where()` names the step in progress, for
    the message and the log when the browser fails."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = launch_context(p, settings)
        try:
            install_navigation_guard(ctx, ui["allowed_hosts"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg = Page(page, ui)
            try:
                return work(pg, ctx)
            except (GeneratorError, Cancelled):
                raise
            except PlaywrightError as e:
                pg.screenshot(settings, f"{jid}-browser-error")
                step = where() if where else ""
                # the error's first line names the call and selector (never page text, prompts or cookies)
                log.warning("job %s browser error%s: %s: %s", jid, f" while {step}" if step else "", type(e).__name__,
                            str(e).splitlines()[0][:200] if str(e) else "")
                raise GenerationError(f"The browser hit an error while talking to {site}"
                                      + (f" ({step})." if step else ".")) from e
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def fetch_src(pg: Page, ctx, src: str) -> bytes | None:
    """The bytes behind an image or video src: a blob is read inside the page, an https URL with the
    signed-in session."""
    if src.startswith("blob:"):
        b64 = pg.page.evaluate(
            """async (u) => { const r = await fetch(u); const b = await r.blob();
               return await new Promise((res, rej) => { const fr = new FileReader();
                 fr.onload = () => res(String(fr.result).split(',')[1]); fr.onerror = rej; fr.readAsDataURL(b); }); }""",
            src)
        return base64.b64decode(b64) if b64 else None
    if src.startswith("https://"):
        r = ctx.request.get(src, timeout=180_000)
        return r.body() if r.ok else None
    return None


def save_video(pg: Page, ctx, src: str, dest: Path, site: str = "Gemini", menu_key: str | None = None) -> None:
    """Write the video behind `src` to `dest` (else click the page's download button, then its `menu_key` entry
    when the button opens a menu), checked to be a video."""
    part = dest.with_suffix(".part")
    data = fetch_src(pg, ctx, src) if src else None
    if data:
        if len(data) > MAX_DOWNLOAD:
            raise GenerationError(f"{site}'s video is larger than 500 MB.")
        part.write_bytes(data)
    else:
        btn = pg.find("download_button", timeout=5)
        if btn is None:
            raise GenerationError(f"Couldn't download the video from {site}.")
        with pg.page.expect_download(timeout=120_000) as d:
            btn.click()
            item = pg.find(menu_key, timeout=3) if menu_key else None
            if item is not None:
                item.click()
        d.value.save_as(str(part))
    with open(part, "rb") as f:
        head = f.read(16)
    if not (head[4:8] == b"ftyp" or head.startswith(b"\x1a\x45\xdf\xa3")):
        part.unlink(missing_ok=True)
        raise GenerationError(f"What {site} returned isn't a video file.")
    os.replace(part, dest)


def cancel_point(should_cancel) -> None:
    if should_cancel():
        raise Cancelled()


# ---- generator -----------------------------------------------------------------
class GeminiBrowserGenerator:
    def __init__(self, settings: WorkerSettings, ui: dict | None = None):
        self.settings = settings
        self.ui = ui or load_ui()
        self.stop = StopText(self.ui)
        self.profile = settings.profile()

    def generate(self, req: ClipRequest, should_cancel: Callable[[], bool]) -> Path:
        started = time.monotonic()
        req.out_dir.mkdir(parents=True, exist_ok=True)
        out = self._in_browser(req.job_id, lambda pg, ctx: self._run(pg, ctx, req, should_cancel))
        log.info("job %s clip ready in %.0fs", req.job_id, time.monotonic() - started)
        return out

    def plan(self, req: PlanRequest, should_cancel: Callable[[], bool]) -> dict:
        started = time.monotonic()
        plan = self._in_browser(req.job_id, lambda pg, ctx: self._run_plan(pg, req, should_cancel))
        log.info("job %s plan ready (%d scenes) in %.0fs", req.job_id, len(plan["scenes"]),
                 time.monotonic() - started)
        return plan

    def check(self, req: CheckRequest, should_cancel: Callable[[], bool]) -> dict:
        started = time.monotonic()
        result = self._in_browser(req.job_id, lambda pg, ctx: self._run_check(pg, req, should_cancel))
        log.info("job %s check done (ok=%s) in %.0fs", req.job_id, result["ok"], time.monotonic() - started)
        return result

    def portrait(self, req: PortraitRequest, should_cancel: Callable[[], bool]) -> Path:
        started = time.monotonic()
        req.out_dir.mkdir(parents=True, exist_ok=True)
        out = self._in_browser(req.job_id, lambda pg, ctx: self._run_portrait(pg, ctx, req, should_cancel))
        log.info("job %s portrait ready in %.0fs", req.job_id, time.monotonic() - started)
        return out

    def _in_browser(self, jid: str, work):
        return run_in_browser(self.settings, self.ui, jid, work)

    _fetch = staticmethod(fetch_src)
    _cancel_point = staticmethod(cancel_point)

    def _download(self, pg: Page, ctx, src: str, dest: Path) -> None:
        save_video(pg, ctx, src, dest)

    # The fixed sequence. Do not add branches that depend on page content beyond stop detection.
    def _run(self, pg: Page, ctx, req: ClipRequest, should_cancel) -> Path:
        jid = req.job_id
        log.info("job %s step=open", jid)
        pg.page.goto(self.ui["app_url"], wait_until="domcontentloaded", timeout=60_000)
        pg.page.wait_for_timeout(2500)

        log.info("job %s step=session", jid)
        check_session(pg, self.stop)
        self._cancel_point(should_cancel)

        # Attach before choosing the Video tool: the upload goes through the same "Upload & tools" menu and
        # can switch the tool off, so the tool is chosen (and confirmed active) last.
        if req.images:
            log.info("job %s step=attach n=%d", jid, len(req.images))
            self._attach(pg, req.images)
        self._cancel_point(should_cancel)

        log.info("job %s step=video_tool", jid)
        self._choose_video_tool(pg)

        label = self.profile.get("model_label")
        if label:
            log.info("job %s step=model", jid)
            self._choose_model(pg, label)
        self._cancel_point(should_cancel)

        # The model pick (or a late upload) can switch the Video tool off; Gemini would then draw a picture.
        if self.ui["selectors"].get("video_tool_active") and pg.find("video_tool_active", timeout=2) is None:
            log.info("job %s step=video_tool (again)", jid)
            self._choose_video_tool(pg)

        log.info("job %s step=aspect %s", jid, req.aspect)
        self._choose_aspect(pg, req.aspect)
        self._cancel_point(should_cancel)

        before = self._send_prompt(pg, req.prompt, jid)
        src = self._wait_for_video(pg, before, should_cancel, jid)
        log.info("job %s step=download", jid)
        dest = req.out_dir / f"attempt-{req.attempt}.mp4"
        self._download(pg, ctx, src, dest)
        return dest

    # The plan sequence: same page and checks, plain chat (no tool), reply is the bible and the script scenes.
    def _run_plan(self, pg: Page, req: PlanRequest, should_cancel) -> dict:
        jid = req.job_id
        log.info("job %s step=open", jid)
        pg.page.goto(self.ui["app_url"], wait_until="domcontentloaded", timeout=60_000)
        pg.page.wait_for_timeout(2500)
        log.info("job %s step=session", jid)
        check_session(pg, self.stop)
        self._cancel_point(should_cancel)
        label = req.model
        # Gemini keeps the picked model for the next chat, and so for the next clip: note it, put it back after.
        previous = self._current_model(pg) if label else ""
        if label:
            log.info("job %s step=model %s", jid, label)
            try:
                self._choose_model(pg, label)
            except GenerationError:  # not offered on this account: write the plan with the current model
                pg.page.keyboard.press("Escape")
                log.warning("job %s plan model %s not available; using the selected one", jid, label)
        try:
            before = self._send_prompt(pg, plan_instruction(req), jid)
            return self._wait_for_plan(pg, before, req.scene_count, should_cancel, jid)
        finally:
            if previous and not model_named(previous, label):
                log.info("job %s step=model back to %s", jid, previous)
                try:
                    self._choose_model(pg, previous)
                except Exception:  # best effort: the clip still runs, on the plan's model
                    log.warning("job %s couldn't switch the model back to %s", jid, previous)

    # The portrait sequence: same page and checks, plain chat (no tool), reply holds one generated image.
    # The check sequence: same page and checks, plain chat (no tool), pictures attached, reply is a JSON verdict.
    def _run_check(self, pg: Page, req: CheckRequest, should_cancel) -> dict:
        jid = req.job_id
        log.info("job %s step=open", jid)
        pg.page.goto(self.ui["app_url"], wait_until="domcontentloaded", timeout=60_000)
        pg.page.wait_for_timeout(2500)
        log.info("job %s step=session", jid)
        check_session(pg, self.stop)
        self._cancel_point(should_cancel)
        log.info("job %s step=attach n=%d", jid, len(req.images))
        self._attach(pg, req.images)
        before = self._send_prompt(pg, req.prompt, jid)
        # Sending with a clip attached brings up Gemini's rights reminder; Agree sends the message on
        self._accept_rights_reminder(pg, timeout=8)
        return self._wait_for_plan(pg, before, 0, should_cancel, jid, parse=parse_check, what="checking the clip")

    def _run_portrait(self, pg: Page, ctx, req: PortraitRequest, should_cancel) -> Path:
        jid = req.job_id
        log.info("job %s step=open", jid)
        pg.page.goto(self.ui["app_url"], wait_until="domcontentloaded", timeout=60_000)
        pg.page.wait_for_timeout(2500)
        log.info("job %s step=session", jid)
        check_session(pg, self.stop)
        self._cancel_point(should_cancel)
        if req.images:  # a scene's first frame: the characters' pictures (and the previous shot's last frame)
            log.info("job %s step=attach n=%d", jid, len(req.images))
            self._attach(pg, req.images)
        before = self._send_prompt(pg, req.prompt, jid)
        img = self._wait_for_image(pg, before, should_cancel, jid)
        log.info("job %s step=download", jid)
        data = self._download_image(pg, img, jid)
        kind = image_kind(data[:16]) if data else None
        if kind is None:
            pg.screenshot(self.settings, f"{jid}-portrait-unreadable")
            raise GenerationError("Couldn't download the portrait from Gemini.")
        if len(data) > MAX_IMAGE:
            raise GenerationError("Gemini's portrait is larger than 20 MB.")
        dest = req.out_dir / f"portrait.{kind}"
        dest.write_bytes(data)
        return dest

    def _download_image(self, pg: Page, img, jid: str) -> bytes | None:
        """The generated image's bytes. Gemini shows it as a blob: URL the page won't fetch(), so: its own
        "Download full size image" button (full resolution), else the pixels of the loaded <img> via a canvas."""
        from playwright.sync_api import Error as PlaywrightError

        response = img.locator("xpath=ancestor::*[self::model-response][1]")
        try:
            img.hover()
            pg.page.wait_for_timeout(500)
            for sel in self.ui["selectors"]["image_download_button"]:
                btn = response.locator(sel)
                if btn.count():
                    with pg.page.expect_download(timeout=60_000) as d:
                        btn.first.click()
                    data = Path(d.value.path()).read_bytes()
                    if image_kind(data[:16]) and len(data) <= MAX_REFERENCE_IMAGE:
                        return data
                    break
        except PlaywrightError as e:
            log.info("job %s full-size download unavailable (%s); using the shown image", jid, type(e).__name__)
        b64 = img.evaluate("""el => { const c = document.createElement('canvas');
            c.width = el.naturalWidth; c.height = el.naturalHeight; c.getContext('2d').drawImage(el, 0, 0);
            return c.toDataURL('image/png').split(',')[1]; }""")
        return base64.b64decode(b64) if b64 else None

    def _wait_for_image(self, pg: Page, before: int, should_cancel, jid: str):
        """Wait for a fully loaded generated image in the newest reply (returns its locator);
        stop on limits, refusals, sign-out."""
        deadline = time.monotonic() + PORTRAIT_TIMEOUT
        img_sel = ", ".join(self.ui["selectors"]["response_image"])
        last_text, stable = None, 0
        while time.monotonic() < deadline:
            self._cancel_point(should_cancel)
            pg.page.wait_for_timeout(POLL_SECONDS * 1000)
            url = pg.page.url
            if (urlparse(url).hostname or "") == "accounts.google.com":
                raise stop_error("signed_out")
            if "/sorry/" in url or pg.matches("captcha"):
                raise stop_error("verification")
            responses = self._responses(pg)
            n = responses.count()
            if n <= before:
                continue
            last = responses.nth(n - 1)
            imgs = last.locator(img_sel)
            for i in range(imgs.count()):
                img = imgs.nth(i)
                if img.evaluate("el => el.complete && el.naturalWidth >= 256"):
                    return img
            try:
                text = last.inner_text(timeout=3000)
            except Exception:
                text = ""
            stable = stable + 1 if text == last_text else 0
            last_text = text
            kind = self.stop.classify(text)
            if kind and stable:
                pg.screenshot(self.settings, f"{jid}-stop-{kind}")
                if kind == "image_limit":
                    raise image_limit_error(pg.body_text())
                raise stop_error(kind)
        pg.screenshot(self.settings, f"{jid}-portrait-timeout")
        raise GenerationError("Gemini didn't draw the portrait in time.")

    def _send_prompt(self, pg: Page, text: str, jid: str) -> int:
        """Type `text` and send it; return how many responses were on the page before."""
        log.info("job %s step=prompt", jid)
        box = pg.find("prompt_input", timeout=10)
        if box is None:
            raise GenerationError("Gemini's prompt box disappeared. Run `python -m worker check-gemini`.")
        box.click()
        pg.page.keyboard.insert_text(text)
        pg.page.wait_for_timeout(500)

        responses = self._responses(pg)
        before = responses.count() if responses is not None else 0
        send = pg.find("send_button", timeout=10)
        if send is None:
            raise GenerationError("Couldn't find Gemini's send button. Run `python -m worker check-gemini`.")
        for _ in range(20):
            if send.is_enabled():
                break
            pg.page.wait_for_timeout(500)
        log.info("job %s step=send", jid)
        send.click()
        return before

    def _wait_for_plan(self, pg: Page, before: int, count: int, should_cancel, jid: str, parse=None,
                       what: str = "writing the scenes") -> dict:
        """Wait until the reply parses as the bible and scene list (or with `parse`, e.g. a clip check) and has
        stopped changing (it streams in)."""
        parse = parse or (lambda text: parse_plan(text, count))
        started = time.monotonic()
        deadline, idle_until = started + PLAN_TIMEOUT, started + PLAN_IDLE
        last_text, error, stable = None, None, 0
        while time.monotonic() < min(deadline, idle_until):
            self._cancel_point(should_cancel)
            pg.page.wait_for_timeout(2000)
            url = pg.page.url
            if (urlparse(url).hostname or "") == "accounts.google.com":
                raise stop_error("signed_out")
            if "/sorry/" in url or pg.matches("captcha"):
                raise stop_error("verification")
            # Gemini's side-by-side vote holds the answer outside the usual reply: read its first choice.
            text = ab_choice_text(pg.body_text(), self.ui["ab_survey"]) if self.ui.get("ab_survey") else None
            if text is None:
                responses = self._responses(pg)
                n = responses.count()
                if n <= before:
                    # No reply yet: still thinking (Stop showing, Pro can think for minutes before its first word) is
                    # progress, so keep waiting within PLAN_TIMEOUT rather than give up after PLAN_IDLE.
                    if pg.find("reply_busy", timeout=0) is not None:
                        idle_until = time.monotonic() + PLAN_IDLE
                    continue
                try:
                    text = responses.nth(n - 1).inner_text(timeout=3000)
                except Exception:
                    continue
            if self.stop.misrouted(text):  # it's making a video instead: don't wait minutes for a script
                pg.screenshot(self.settings, f"{jid}-plan-misrouted")
                raise GenerationError("Gemini started making a video instead of writing the script.")
            try:
                scenes = parse(text)
            except GenerationError as e:
                scenes, error = None, e
            stable = stable + 1 if text == last_text else 0
            # Still writing, or thinking (Pro shows one fixed heading such as "Creating two scenes" for minutes,
            # with Stop and "Answer now" up): keep waiting, within PLAN_TIMEOUT.
            busy = scenes is None and pg.find("reply_busy", timeout=0) is not None
            if text != last_text or busy:
                idle_until = time.monotonic() + PLAN_IDLE
            last_text = text
            if scenes and stable:
                return scenes
            if scenes is None and stable and text.strip() and not busy:
                kind = self.stop.classify(text)  # a finished reply that isn't a list: limit, refusal, …
                if kind:
                    pg.screenshot(self.settings, f"{jid}-stop-{kind}")
                    if kind == "error":
                        raise GenerationError(f"Gemini's chat answered with an error (“something went wrong”) "
                                              f"while {what}. That's on Gemini's side: retry in a while.")
                    raise stop_error(kind)
                if stable >= 3:  # unchanged for ~6 s and still not a list: Gemini is done, just wrong
                    pg.screenshot(self.settings, f"{jid}-plan-unreadable")
                    raise error
        pg.screenshot(self.settings, f"{jid}-plan-timeout")
        raise error or GenerationError(f"Gemini didn't finish {what} in time.")

    def _raise_from_text(self, text: str, fallback: GeneratorError) -> None:
        kind = self.stop.classify(text)
        if kind:
            raise stop_error(kind)
        raise fallback

    def _choose_video_tool(self, pg: Page) -> None:
        tool = find_video_tool(pg)
        if tool is None:
            pg.screenshot(self.settings, "no-video-tool")
            self._raise_from_text(pg.body_text(), GenerationError(
                "Couldn't find Gemini's Video tool. Run `python -m worker check-gemini`."))
        disabled = tool.is_disabled() or (tool.get_attribute("aria-disabled") == "true")
        if disabled:
            pg.screenshot(self.settings, "video-tool-disabled")
            self._raise_from_text(pg.body_text(), GenerationError("Gemini's Video tool is unavailable right now."))
        tool.click()
        pg.page.wait_for_timeout(800)
        if self.ui["selectors"].get("video_tool_active") and pg.find("video_tool_active", timeout=5) is None:
            pg.screenshot(self.settings, "video-tool-not-active")
            self._raise_from_text(pg.body_text(), GenerationError(
                "Couldn't confirm the Video tool is selected. Run `python -m worker check-gemini`."))

    def _choose_aspect(self, pg: Page, aspect: str) -> None:
        """Pick Landscape (16:9) or Portrait (9:16) in the Video tool's aspect menu: Gemini ignores the format
        written in the prompt and makes 16:9 unless this is set. Landscape is Gemini's default, so without the
        menu a 16:9 clip still goes ahead; a 9:16 one stops rather than spend a video on the wrong shape."""
        word = ASPECT_NAMES.get(aspect, "")
        if not word:
            return
        switch = pg.find("aspect_switch", timeout=5)
        if switch is None:
            if aspect == "16:9":
                return
            pg.screenshot(self.settings, "no-aspect-menu")
            raise GenerationError("Couldn't find Gemini's aspect ratio menu to choose Portrait (9:16). "
                                  "Run `python -m worker check-gemini`.")
        if word.lower() in (switch.inner_text() or "").lower():
            return
        switch.click()
        pg.page.wait_for_timeout(600)
        option = pg.find("aspect_option", timeout=5, label=aspect)
        if option is None:
            pg.page.keyboard.press("Escape")
            pg.screenshot(self.settings, "no-aspect-option")
            raise GenerationError(f"Gemini's aspect ratio menu has no {word} ({aspect}) option.")
        option.click()
        pg.page.wait_for_timeout(600)
        switch = pg.find("aspect_switch", timeout=5)
        if switch is None or word.lower() not in (switch.inner_text() or "").lower():
            pg.screenshot(self.settings, "aspect-not-set")
            raise GenerationError(f"Gemini didn't switch the video to {word} ({aspect}).")

    def _current_model(self, pg: Page) -> str:
        """The model the picker shows ("Flash", "Pro", …), or "" without a picker."""
        switch = pg.find("model_switch", timeout=5)
        return (switch.inner_text() or "").strip() if switch else ""

    def _choose_model(self, pg: Page, label: str) -> None:
        switch = pg.find("model_switch", timeout=5)
        if switch is None:
            raise GenerationError(f"Couldn't find Gemini's model picker to choose “{label}”.")
        if model_named(switch.inner_text() or "", label):
            return
        switch.click()
        pg.page.wait_for_timeout(600)
        option = self._model_option(pg, label)
        if option is None:
            pg.page.keyboard.press("Escape")
            raise GenerationError(f"Model “{label}” isn't offered in Gemini. Check GEMINI_MODEL_LABEL.")
        option.click()
        pg.page.wait_for_timeout(800)
        switch = pg.find("model_switch", timeout=5)
        if switch is None or not model_named(switch.inner_text() or "", label):
            raise GenerationError(f"Gemini didn't switch to model “{label}”.")

    @staticmethod
    def _model_option(pg: Page, label: str):
        """The menu entry for exactly `label`: "Flash" is "3.8 Flash", never "3.5 Flash-Lite" listed above it."""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for s in pg.sel.get("model_option", []):
                loc = pg.page.locator(s.format(label=label))
                try:
                    for i in range(min(loc.count(), 8)):
                        item = loc.nth(i)
                        if item.is_visible() and model_named((item.inner_text() or "").split("\n")[0], label):
                            return item
                except Exception:
                    continue
            pg.page.wait_for_timeout(400)
        return None

    def _accept_rights_reminder(self, pg: Page, timeout: float = 1.0) -> bool:
        """Click Agree on Gemini's "A reminder about creating videos" (you have the rights to what you upload),
        if it's showing: approved by the account owner, like the image-upload consent."""
        agree = pg.find("rights_reminder_agree", timeout=timeout)
        if agree is None:
            return False
        log.info("step=rights_reminder")
        agree.click()
        pg.page.wait_for_timeout(800)
        return True

    def _accept_upload_consent(self, pg: Page, timeout: float = 1.0) -> bool:
        """Click Agree on Gemini's one-time image-upload consent dialog, if it's showing."""
        agree = pg.find("upload_consent_agree", timeout=timeout)
        if agree is None:
            return False
        log.info("step=upload_consent")
        agree.click()
        pg.page.wait_for_timeout(800)
        return True

    def _open_file_chooser(self, pg: Page, files: list[str]) -> None:
        """Upload button → "Upload files" → file chooser, accepting the consent dialog if it appears."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        def click_upload_item(timeout_ms: int):
            btn = pg.find("upload_button", timeout=5)
            if btn is None:
                raise GenerationError("Couldn't find Gemini's upload button for the reference images.")
            btn.click()
            item = pg.find("upload_menu_item", timeout=5)
            if item is None:
                raise GenerationError("Couldn't find Gemini's “Upload files” option.")
            with pg.page.expect_file_chooser(timeout=timeout_ms) as fc:
                item.click()
            return fc.value

        try:
            chooser = click_upload_item(5_000)
        except PlaywrightTimeoutError:
            agree = pg.find("upload_consent_agree", timeout=2)
            if agree is None:
                raise
            log.info("step=upload_consent")
            try:  # Agree may open the file chooser itself
                with pg.page.expect_file_chooser(timeout=5_000) as fc:
                    agree.click()
                chooser = fc.value
            except PlaywrightTimeoutError:
                chooser = click_upload_item(10_000)
        chooser.set_files(files)

    def _attach(self, pg: Page, images: list[Path]) -> None:
        files = [str(p) for p in images]
        inp = pg.page.locator(self.ui["selectors"]["file_input"][0])
        if inp.count():
            inp.first.set_input_files(files)
        else:
            self._open_file_chooser(pg, files)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            for s in self.ui["selectors"]["attachment_preview"]:
                if pg.page.locator(s).count() >= len(files):
                    pg.page.wait_for_timeout(1000)
                    return
            self._accept_upload_consent(pg, timeout=0)  # the dialog can also follow a direct upload
            self._accept_rights_reminder(pg, timeout=0)
            pg.page.wait_for_timeout(1000)
        raise GenerationError("Gemini didn't accept the reference images within a minute.")

    def _responses(self, pg: Page):
        for s in self.ui["selectors"]["response"]:
            loc = pg.page.locator(s)
            if loc.count():
                return loc
        return pg.page.locator(self.ui["selectors"]["response"][0])

    def _wait_for_video(self, pg: Page, before: int, should_cancel, jid: str) -> str:
        timeout = self.settings.GENERATION_TIMEOUT_SECONDS
        deadline = time.monotonic() + timeout
        video_sel = ", ".join(self.ui["selectors"]["response_video"])
        while True:
            self._cancel_point(should_cancel)
            if time.monotonic() > deadline:
                pg.screenshot(self.settings, f"{jid}-timeout")
                raise GenerationError(f"Gemini didn't finish the video within {timeout // 60} minutes.")
            pg.page.wait_for_timeout(POLL_SECONDS * 1000)
            url = pg.page.url
            if (urlparse(url).hostname or "") == "accounts.google.com":
                raise stop_error("signed_out")
            if "/sorry/" in url or pg.matches("captcha"):
                raise stop_error("verification")
            responses = self._responses(pg)
            n = responses.count()
            if n <= before:
                continue
            last = responses.nth(n - 1)
            vids = last.locator(video_sel)
            if vids.count():
                v = vids.first
                src = v.get_attribute("src") or ""
                if not src:
                    source = v.locator("source")
                    src = (source.first.get_attribute("src") or "") if source.count() else ""
                if src:
                    return src
                continue
            if self._has_drawn_image(last):
                pg.screenshot(self.settings, f"{jid}-image-not-video")
                raise GenerationError("Gemini made a picture instead of a video (its Video tool wasn't on).")
            try:
                text = last.inner_text(timeout=3000)
            except Exception:
                text = ""
            kind = self.stop.classify(text)
            if kind:
                pg.screenshot(self.settings, f"{jid}-stop-{kind}")
                raise stop_error(kind)

    @staticmethod
    def _has_drawn_image(reply) -> bool:
        """True when the reply holds a fully loaded picture (not an icon or a loading placeholder)."""
        imgs = reply.locator(REPLY_IMAGE)
        return any(imgs.nth(i).evaluate("el => el.complete && el.naturalWidth >= 256") for i in range(imgs.count()))


def find_video_tool(pg: Page):
    """The Video tool button, opening the Tools menu (and its "More tools") if needed; None if absent."""
    tool = pg.find("video_tool", timeout=2)
    if tool is None:
        btn = pg.find("tools_button", timeout=5)
        if btn is not None:
            btn.click()
            pg.page.wait_for_timeout(800)
            tool = pg.find("video_tool", timeout=4)
            if tool is None:
                more = pg.find("more_tools", timeout=2)
                if more is not None:
                    more.click()
                    pg.page.wait_for_timeout(800)
                    tool = pg.find("video_tool", timeout=4)
    return tool


# ---- diagnostics / session check ---------------------------------------------
def inspect(settings: WorkerSettings, ui: dict | None = None, open_tools: bool = True) -> dict:
    """Open Gemini, classify the session, report which selectors match, save a screenshot.

    Read-only: it may open the Tools menu to look for the Video tool, then presses Escape. Also reports
    `plan` ("free"/"paid", from the Upgrade button) and `video` ("available"/"unavailable").
    """
    from playwright.sync_api import sync_playwright

    ui = ui or load_ui()
    stop = StopText(ui)
    result: dict = {"state": "needs_attention", "message": "", "model": None, "plan": None, "video": None,
                    "selectors": {}, "screenshot": None}
    with sync_playwright() as p:
        ctx = launch_context(p, settings, headless=True)  # a check needs no window on screen
        try:
            install_navigation_guard(ctx, ui["allowed_hosts"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg = Page(page, ui)
            page.goto(ui["app_url"], wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(3000)
            try:
                check_session(pg, stop)
                result["state"], result["message"] = "connected", "Signed in to Gemini."
            except SignedOut as e:
                result["state"], result["message"] = "signed_out", e.user_message
            except LimitReached as e:
                result["state"], result["message"] = "limit_reached", e.user_message
            except (AccountStop, GeneratorError) as e:
                result["message"] = e.user_message
            sw = pg.find("model_switch", timeout=2)
            if sw is not None:
                result["model"] = (sw.inner_text() or "").strip().splitlines()[0][:40] or None
            for key in ui["selectors"]:
                if key == "model_option":
                    continue
                result["selectors"][key] = pg.matches(key)
            if result["state"] == "connected":
                # Free accounts get an "Upgrade" button; paid (Google AI Pro/Ultra) accounts don't.
                result["plan"] = "free" if pg.find("upgrade_button", timeout=2) is not None else "paid"
                if open_tools:
                    tool = find_video_tool(pg)
                    if tool is None:
                        result["video"] = "unavailable"
                    else:
                        result["selectors"]["video_tool"] = pg.matches("video_tool")
                        off = tool.is_disabled() or tool.get_attribute("aria-disabled") == "true"
                        result["video"] = "unavailable" if off else "available"
                    page.keyboard.press("Escape")
                    page.keyboard.press("Escape")
            result["screenshot"] = pg.screenshot(settings, "check")
        finally:
            ctx.close()
    return result


def open_plain_window(settings: WorkerSettings, ui: dict | None = None) -> subprocess.Popen:
    ui = ui or load_ui()
    cmd = plain_browser_command(settings, ui)
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
