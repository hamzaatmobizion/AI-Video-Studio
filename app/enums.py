from enum import StrEnum


class Aspect(StrEnum):
    PORTRAIT = "9:16"
    LANDSCAPE = "16:9"


class Provider(StrEnum):
    """Where a video's clips are made. Scripts and clip checks are Gemini's chat either way."""
    GEMINI = "gemini"  # Gemini web app, Veo: 10-second clips from the daily video limit
    FLOW = "flow"  # Google Flow, Veo 3.1 - Lite: 8-second clips paid with Flow credits


SCENE_SECONDS = {Provider.GEMINI: 10, Provider.FLOW: 8}  # one scene = one clip of this length
PROVIDER_NAMES = {Provider.GEMINI: "Gemini", Provider.FLOW: "Google Flow"}


class GenStatus(StrEnum):
    PLANNING = "PLANNING"  # Gemini (chat) is writing the scene prompts from the user's one prompt
    REVIEW = "REVIEW"  # the script is drafted; waits for the user to edit and approve it
    GENERATING = "GENERATING"  # sent to the worker, waiting for the clip
    SAVING = "SAVING"  # clip downloaded, uploading to Drive
    READY = "READY"
    SAVE_FAILED = "SAVE_FAILED"  # clip kept locally; "Try saving again"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"  # Gemini stopped (e.g. daily limit); finished scenes kept, continues after Resume


ACTIVE = {GenStatus.PLANNING, GenStatus.GENERATING, GenStatus.SAVING}
