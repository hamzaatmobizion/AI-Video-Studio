from enum import StrEnum


class Aspect(StrEnum):
    PORTRAIT = "9:16"
    LANDSCAPE = "16:9"


class GenStatus(StrEnum):
    GENERATING = "GENERATING"  # sent to the worker, waiting for the clip
    SAVING = "SAVING"  # clip downloaded, uploading to Drive
    READY = "READY"
    SAVE_FAILED = "SAVE_FAILED"  # clip kept locally; "Try saving again"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ACTIVE = {GenStatus.GENERATING, GenStatus.SAVING}
