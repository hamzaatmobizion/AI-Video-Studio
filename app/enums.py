from enum import StrEnum


class Aspect(StrEnum):
    PORTRAIT = "9:16"
    LANDSCAPE = "16:9"


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
