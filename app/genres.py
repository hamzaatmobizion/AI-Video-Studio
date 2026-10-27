"""Video types and their rule packs: how a kids' lesson, an ad or a short film should be shaped.

The user only writes a prompt; the type is read from it (or picked in the Studio), and its rules go to Gemini's
planner (`plan_rules`) and its things-to-avoid to every clip prompt (`clip_avoid`). Rules every video gets, whatever
its type, live in worker/gemini_browser.py `plan_instruction`.
"""
import re

GENRES = {
    "general": {
        "label": "General",
        "arc": ["Hook", "Build", "Turn", "Payoff"],
        "rules": [],
        "avoid": [],
    },
    "kids": {
        "label": "Kids / educational",
        "words": r"kids?|children|child|toddlers?|preschool\w*|nursery|kindergarten|alphabet|abc|phonics|"
                 r"counting|numbers? for|learn(?:ing)? (?:the )?(?:letters?|colou?rs|shapes|numbers)|ages? \d",
        "arc": ["Hook", "Learn", "Repeat", "Celebrate"],
        "rules": [
            "Made for young children: friendly, bright and gentle; nothing scary, dangerous or sad.",
            "Teach one new idea per scene and repeat the key word in it ('A! A! Apple!').",
            "The host talks to the viewer and invites them to join in ('Say it with me!', 'Can you point?').",
            "Slow, smooth camera and simple actions a child can follow; no fast cuts or sudden movement.",
            "The last scene recaps everything taught, praises the viewer and waves goodbye.",
            "Short, simple sentences: at most about 20 spoken words per scene.",
        ],
        "avoid": ["scary faces", "sharp teeth", "danger", "violence"],
    },
    "story": {
        "label": "Story / short film",
        "words": r"story|short film|tale|drama|adventure|journey|mystery|fairy ?tale|plot|thriller|romance",
        "arc": ["Setup", "Conflict", "Turn", "Resolution"],
        "rules": [
            "Give the main character a clear want in scene 1, an obstacle in the middle and a resolution at the end.",
            "Show feelings through faces and actions, not by saying them.",
            "Let one prop, place or line return at the end to close the story.",
        ],
        "avoid": [],
    },
    "ad": {
        "label": "Ad / product",
        "words": r"\bads?\b|advert\w*|commercial|promo\w*|product|brand|launch|sale|offer|marketing|"
                 r"call to action|buy now",
        "arc": ["Hook", "Problem", "Product", "Benefit", "Call to action"],
        "rules": [
            "Grab attention in the first 2 seconds with a striking image or question.",
            "Show the product clearly, the same shape, colour and label in every scene it appears in.",
            "One benefit per scene, shown in use rather than only said.",
            "End on a clean hero shot of the product and a short spoken call to action.",
        ],
        "avoid": ["competitor logos", "cluttered background"],
    },
    "explainer": {
        "label": "Explainer / tutorial",
        "words": r"how to|how-to|tutorial|explain\w*|step[- ]by[- ]step|guide|recipe|lesson|demonstrat\w*|tips",
        "arc": ["Question", "Step", "Step", "Result"],
        "rules": [
            "Open with the question or result the viewer wants, then one step per scene, in order.",
            "Keep the presenter and the work area the same in every scene; show each step clearly in a medium "
            "or close shot.",
            "Close on the finished result and a one-line summary.",
        ],
        "avoid": [],
    },
    "comedy": {
        "label": "Comedy / skit",
        "words": r"comedy|comic|funny|jokes?|skit|prank|meme|hilarious|sitcom|stand-?up",
        "arc": ["Setup", "Build", "Twist", "Punchline"],
        "rules": [
            "Keep every joke and every written line word for word: the wording is the joke.",
            "Big, readable facial reactions; let a beat of silence or a reaction land after each punchline.",
            "Save the biggest punchline for the last scene and end on everyone's reaction to it.",
        ],
        "avoid": [],
    },
    "music": {
        "label": "Music / dance",
        # not "music" or "beat" alone: a comedy asking for a dhol beat underneath isn't a music video
        "words": r"music video|songs?|dance|dancing|dancers?|choreograph\w*|singing|singers?|rap(?:per)?s?|"
                 r"concert|lyrics",
        "arc": ["Intro", "Verse", "Chorus", "Finale"],
        "rules": [
            "Keep the same rhythm and energy from scene to scene, building to the biggest moment in the last "
            "chorus or finale.",
            "One dance move or performance beat per scene, at a speed the generator can show cleanly.",
        ],
        "avoid": [],
    },
    "social": {
        "label": "Social / vlog",
        "words": r"vlog|ugc|testimonial|talking head|selfie|day in (?:my|the) life|reaction|unboxing|"
                 r"influencer|tiktok|reels?|shorts",
        "arc": ["Hook", "Moment", "Moment", "Sign-off"],
        "rules": [
            "Talk straight to the camera, casual and personal; hook the viewer in the first 2 seconds.",
            "Handheld or selfie framing that stays steady on the face.",
            "End with a quick sign-off or question to the viewer.",
        ],
        "avoid": [],
    },
}
GENRE_LABELS = {k: g["label"] for k, g in GENRES.items()}
_WORDS = [(k, re.compile(rf"\b(?:{g['words']})\b", re.I)) for k, g in GENRES.items() if g.get("words")]


def genre_from_prompt(prompt: str) -> str:
    """The video type a prompt reads as: the type whose words it uses most (an early word breaks a tie), else
    'general'."""
    text = prompt or ""
    best, score = "general", (0, 0)
    for key, words in _WORDS:
        hits = words.findall(text)
        if hits:
            first = words.search(text).start()
            if (len(hits), -first) > score:
                best, score = key, (len(hits), -first)
    return best


def valid_genre(genre: str, prompt: str) -> str:
    """The user's choice when it's a known type, else the type read from the prompt."""
    genre = (genre or "").strip().lower()
    return genre if genre in GENRES else genre_from_prompt(prompt)


def plan_rules(genre: str, scene_count: int) -> str:
    """The type's rules for Gemini's planner: its story shape spread over the scenes, then its own rules."""
    g = GENRES.get(genre) or GENRES["general"]
    lines = [f"This is a {g['label'].lower()} video. Shape it as: {_arc(g['arc'], scene_count)}."]
    lines += g["rules"]
    return "\n".join(f"- {x}" for x in lines)


def clip_avoid(genre: str) -> list[str]:
    return list((GENRES.get(genre) or GENRES["general"])["avoid"])


def _arc(beats: list[str], n: int) -> str:
    """The beats over `n` scenes: the first and last beat keep their place, the middle ones stretch or merge."""
    if n <= 1:
        return " → ".join(beats) + " (all in one scene)"
    if n == 2:
        return f"scene 1 = {beats[0]}, scene 2 = {beats[-1]}"
    middle = beats[1:-1]
    out = [beats[0]] + [middle[min(i * len(middle) // (n - 2), len(middle) - 1)] for i in range(n - 2)] + [beats[-1]]
    return ", ".join(f"scene {i} = {b}" for i, b in enumerate(out, 1))
