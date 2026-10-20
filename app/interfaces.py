"""Swappable provider contracts. Implementations live in app/providers, selected in app/factories."""
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


# ---- video generator ----------------------------------------------------
class GeneratorError(Exception):
    """`final=True`: give up now (4xx). `final=False`: transient, retry on the next poll."""

    def __init__(self, message: str, final: bool):
        super().__init__(message)
        self.message = message
        self.final = final


@dataclass
class JobStatus:
    status: str  # queued | running | done | failed
    error: str | None = None
    video_url: str | None = None
    final_prompt: str | None = None
    scenes: list | None = None  # plan jobs: the script scenes (dicts) Gemini wrote; older workers send strings


@dataclass
class WorkerHealth:
    ok: bool
    message: str
    details: dict = field(default_factory=dict)


class VideoGenerator(Protocol):
    def submit(self, prompt: str, aspect: str, ref: str, images: list[Path]) -> str: ...
    def plan(self, prompt: str, aspect: str, ref: str, scenes: int, cast: list[dict] | None = None,
             style: str = "", language: str = "") -> str: ...
    def check(self, job_id: str) -> JobStatus: ...
    def download(self, video_url: str, dest: Path) -> int: ...
    def cancel(self, job_id: str) -> None: ...
    def ping(self) -> WorkerHealth: ...


# ---- storage ------------------------------------------------------------
class StorageError(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.message = message
        self.retryable = retryable


@dataclass
class StoredFile:
    file_id: str
    size: int


@dataclass
class RangeStream:
    chunks: Iterator[bytes]
    status: int  # 200 or 206
    headers: dict[str, str]
    close: callable


class VideoStorage(Protocol):
    def save(self, local_path: Path, name: str, subfolder: str) -> StoredFile: ...
    def get(self, file_id: str) -> dict: ...
    def open(self, file_id: str, range_header: str | None) -> RangeStream: ...
    def delete(self, file_id: str) -> None: ...
