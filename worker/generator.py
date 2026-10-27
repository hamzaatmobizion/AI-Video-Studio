"""Generator contract and typed errors. The pipeline reacts to the error type, never to page text."""
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class ClipRequest:
    job_id: str
    attempt: int
    prompt: str
    aspect: str
    images: list[Path] = field(default_factory=list)
    out_dir: Path = Path(".")
    frame: int | None = None  # which of `images` is the scene's start frame (Flow's Frames to Video), if any
    project_url: str = ""  # Flow: the video's project to work in (empty: a new one); set to the project used


@dataclass
class PlanRequest:
    """Ask Gemini (chat, not the Video tool) to write a `scene_count`-scene script from one idea.
    `cast`: [{name, appearance, outfit, voice, personality}] the script must use; `rules`: the video type's rules
    (app/genres.py), added to the planner's own."""

    job_id: str
    prompt: str
    aspect: str
    scene_count: int
    clip_seconds: int = 10
    cast: list[dict] = field(default_factory=list)
    style: str = ""
    language: str = ""
    rules: str = ""
    attempt: int = 1
    model: str = ""  # the chat model to write it with ("" = whatever is selected in Gemini)


@dataclass
class PortraitRequest:
    """Ask for one image from a description: a character portrait, or a scene's first frame drawn from the attached
    `images` (the characters' pictures, then the previous shot's last frame). Gemini's chat draws it, or Google Flow's
    Image mode for a Flow video (`aspect` is then the frame's shape)."""

    job_id: str
    prompt: str
    out_dir: Path = Path(".")
    images: list[Path] = field(default_factory=list)
    aspect: str = ""
    project_url: str = ""  # Flow: the video's project to work in (empty: a new one); set to the project used


@dataclass
class CheckRequest:
    """Ask Gemini (chat, text only) whether a new clip's frames match the attached reference pictures."""

    job_id: str
    prompt: str
    images: list[Path] = field(default_factory=list)


class GeneratorError(Exception):
    """Base. `user_message` is shown to the owner verbatim."""

    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


class GenerationError(GeneratorError):
    """Something went wrong making this clip; a bounded retry is allowed."""


class SafetyRefusal(GeneratorError):
    """Gemini declined the prompt. Never reworded or retried automatically."""


class AccountStop(GeneratorError):
    """Stops the provider's generation (Gemini or Google Flow) until the owner resumes."""

    kind = "account_stop"


class LimitReached(AccountStop):
    kind = "limit_reached"


class SignedOut(AccountStop):
    kind = "authentication_required"


class VerificationRequired(AccountStop):
    kind = "manual_intervention_required"


class SubscriptionRequired(AccountStop):
    kind = "subscription_required"


class GeminiBusy(GenerationError):
    """Gemini says it's at full capacity: retried, after a longer wait than other errors."""


class ImageLimitReached(GeneratorError):
    """Gemini's image quota is used up. Fails the portrait only: videos have their own quota, so nothing pauses."""


class Cancelled(Exception):
    pass


class VideoGenerator(Protocol):
    profile: dict

    def generate(self, req: ClipRequest, should_cancel: Callable[[], bool]) -> Path:
        """Produce one clip file inside req.out_dir and return its path."""
        ...

    def plan(self, req: PlanRequest, should_cancel: Callable[[], bool]) -> dict | list[dict]:
        """Return {"bible", "scenes"} with exactly req.scene_count script scenes written by Gemini (see parse_plan);
        a bare list of scenes is taken as having no bible."""
        ...

    def portrait(self, req: PortraitRequest, should_cancel: Callable[[], bool]) -> Path:
        """Produce one JPG/PNG/WebP portrait inside req.out_dir and return its path."""
        ...

    def check(self, req: CheckRequest, should_cancel: Callable[[], bool]) -> dict:
        """Return {"ok": bool, "problems": str}: whether the clip's frames match the references."""
        ...
