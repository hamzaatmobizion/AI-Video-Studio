"""Checks on a prompt and its picked characters before anything goes to Gemini: what is known to make Gemini refuse
a clip, or to make the video ignore what the prompt asks for. Shown in the Studio as notes; they never block."""
import math
import re

from app.script import ANIMATED, style_from_prompt
from app.timeline import story_moments

# A character who may read as a child to Gemini, in a moment its child-safety rules refuse: Gemini then declines
# the clip, often only as "I encountered an error"
YOUNG = re.compile(r"\b(young|boy|son|beta|kid|teen\w*|child|little|baby[- ]?faced)\b", re.I)
ADULT = re.compile(r"\b(in (?:his|her|their) (?:early |mid |late )?[2-9]0s|\d{2}[- ]?(?:years?[- ]old|yrs?|year)|"
                   r"mustache|moustache|beard|stubble|grown[- ]up|adult|middle[- ]aged|uncle|husband|father|dad)\b",
                   re.I)
# "a chubby young Punjabi man": the prompt itself says adult, so the planner keeps them clearly adult (no note)
YOUNG_ADULT = re.compile(r"\byoung(?:\s+\w+){0,2}\s+(?:man|men|woman|women|lady|guy|husband|wife|groom|bride)\b",
                         re.I)
SENSITIVE = re.compile(r"\b(kiss\w*|romanc\w*|romantic|jaan|darling|in love|flirt\w*|slipper|chappal|beat\w*|"
                       r"hit\w*|slap\w*|spank\w*|smack\w*|whack\w*|thrash\w*)\b", re.I)
MOMENTS_PER_CLIP = 2  # more of the story's moments in one 10-second clip and the motion gets rushed and messy

GURMUKHI = re.compile(r"[਀-੿]")
DEVANAGARI = re.compile(r"[\u0900-\u0963\u0966-\u097F]")  # not । ॥, which Gurmukhi uses too
PAKISTANI = re.compile(r"\b(pakistan\w*|shahmukhi|urdu|lahor\w*)\b", re.I)
REAL_LOOK = re.compile(r"\b(photo-?realistic|realistic|real (?:people|person|actors?)|live[- ]action|photographic)\b",
                       re.I)
# Colour words: an outfit saved without any lets Veo pick a new colour in every clip
COLOURS = re.compile(r"\b(red|maroon|pink|orange|yellow|golden|gold|green|mehandi|olive|teal|blue|navy|purple|violet|"
                     r"brown|beige|cream|white|black|grey|gray|silver|multicolou?r\w*|striped|checked|floral)\b", re.I)


def prompt_checks(prompt: str, cast: list[dict], style: str = "", language: str = "", seconds: int = 0) -> list[str]:
    """The notes' texts (see prompt_notes)."""
    return [n["text"] for n in prompt_notes(prompt, cast, style, language, seconds)]


def prompt_notes(prompt: str, cast: list[dict], style: str = "", language: str = "", seconds: int = 0) -> list[dict]:
    """Notes for the user about `prompt`, the picked `cast` (library sheets), the chosen style and language, and the
    video's length in `seconds`: [{"level": "error" | "warning", "text"}]. An error is something Gemini is likely to
    refuse or fail on; a warning, something that may spoil the video."""
    notes = []

    def note(level: str, text: str) -> None:
        notes.append({"level": level, "text": text})

    adult_young = YOUNG_ADULT.search(prompt or "")
    if adult_young and SENSITIVE.search(prompt or "") and not ADULT.search(prompt or ""):
        note("warning", f"“{adult_young.group(0)}” in a story with romance or hitting: the tool gives him or her a "
                        "grown-up look, but a cartoon can still come out childlike, and Gemini then refuses the "
                        "scene. Safest: add an adult feature (“in his 30s, with a thick mustache”) or pick a "
                        "character with an adult portrait.")
    young = YOUNG.search(YOUNG_ADULT.sub(" ", prompt or ""))
    if young and SENSITIVE.search(prompt or "") and not ADULT.search(prompt or ""):
        note("error", f"Someone is described as “{young.group(0)}” with no adult age, in a story with romance or "
                     "hitting. Gemini may take them for a child and refuse the clip, often only saying “I "
                     "encountered an error”. Make their age clear, e.g. “a chubby man in his 30s with a thick "
                     "mustache”.")
    moments, clips = story_moments(prompt), max(math.ceil(seconds / 10), 1)
    if seconds and moments > clips * MOMENTS_PER_CLIP:
        need = min(math.ceil(moments / MOMENTS_PER_CLIP), 12) * 10
        note("warning", f"Your story sets out {moments} moments, but {seconds} seconds is {clips} clip"
                     f"{'s' if clips > 1 else ''} of 10 seconds, so each clip has to fit about "
                     f"{math.ceil(moments / clips)} of them. That rushes the action: blurred hands, sudden camera "
                     f"changes and things vanishing are likely. Choose {need} seconds, or fewer moments.")
    wants = f"{prompt} {language}"
    if PAKISTANI.search(wants):
        if GURMUKHI.search(prompt):
            note("warning", "Some lines are in Gurmukhi (ਪੰਜਾਬੀ), the script used for Punjabi in India, so "
                            "they're likely to be spoken with Indian pronunciation. Write them in Shahmukhi "
                            "(پنجابی) for a Pakistani accent.")
        if DEVANAGARI.search(prompt):
            note("warning", "Some lines are in Devanagari (Hindi script). Write them in Urdu or Shahmukhi script "
                            "for Pakistani pronunciation.")
    # Only for characters without a picture: a picture carries the look of the ones that have one
    vague = [c["name"] for c in cast if not c.get("image_path") and (c.get("outfit") or "").strip()
             and not COLOURS.search(c["outfit"])]
    if vague:
        note("warning", f"{', '.join(vague)}: no picture, and the saved outfit names no colours, so each clip may "
                        "dress them differently. Add a portrait, or colours and patterns (e.g. 'red and teal striped "
                        "sari').")
    look = style or style_from_prompt(prompt)
    pictured = [c["name"] for c in cast if c.get("image_path")]
    if pictured and REAL_LOOK.search(f"{look} {prompt}") and not (ANIMATED.search(look) and not REAL_LOOK.search(look)):
        note("error", f"The video asks for a realistic look while pictures of {', '.join(pictured)} go with the clips. "
                     "Gemini often refuses clips that look like real people made from pictures. A cartoon style "
                     "(e.g. Pixar-style 3D animation) and cartoon portraits avoid that.")
    return notes
