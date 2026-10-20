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


@dataclass
class PlanRequest:
    """Ask Gemini (chat, not the Video tool) to write a `scene_count`-scene script from one idea.
    `cast`: [{name, appearance, outfit, voice, personality}] the script must use."""

    job_id: str
    prompt: str
    aspect: str
    scene_count: int
    clip_seconds: int = 10
    cast: list[dict] = field(default_factory=list)
    style: str = ""
    language: str = ""


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
    """Stops ALL generation (one shared account) until the owner resumes."""

    kind = "account_stop"


class LimitReached(AccountStop):
    kind = "limit_reached"


class SignedOut(AccountStop):
    kind = "authentication_required"


class VerificationRequired(AccountStop):
    kind = "manual_intervention_required"


class SubscriptionRequired(AccountStop):
    kind = "subscription_required"


class Cancelled(Exception):
    pass


class VideoGenerator(Protocol):
    profile: dict

    def generate(self, req: ClipRequest, should_cancel: Callable[[], bool]) -> Path:
        """Produce one clip file inside req.out_dir and return its path."""
        ...

    def plan(self, req: PlanRequest, should_cancel: Callable[[], bool]) -> list[dict]:
        """Return exactly req.scene_count script scenes written by Gemini (see parse_script)."""
        ...
