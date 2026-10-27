from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Generation(Base):
    __tablename__ = "generations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    prompt: Mapped[str] = mapped_column(Text)
    aspect: Mapped[str] = mapped_column(String(8))
    ref_images: Mapped[list] = mapped_column(JSON, default=list)  # paths under var/refs
    status: Mapped[str] = mapped_column(String(20), index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    poll_errors: Mapped[int] = mapped_column(Integer, default=0)
    temp_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    upload_attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_upload_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    drive_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)
    planned: Mapped[bool] = mapped_column(Boolean, default=False)  # Gemini split `prompt` into the scenes
    review: Mapped[bool] = mapped_column(Boolean, default=False)  # stop at REVIEW once the script is drafted
    cast_ids: Mapped[list] = mapped_column(JSON, default=list)  # Character ids the script may use
    style: Mapped[str] = mapped_column(Text, default="")  # visual style for every clip
    language: Mapped[str] = mapped_column(String(40), default="")  # language of the spoken lines
    genre: Mapped[str] = mapped_column(String(30), default="")  # video type (app/genres.py)
    bible: Mapped[dict] = mapped_column(JSON, default=dict)  # story bible Gemini wrote with the script
    music_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # one track under the whole video
    lock_frames: Mapped[bool] = mapped_column(Boolean, default=False)  # each scene starts from a still made first
    check_clips: Mapped[bool] = mapped_column(Boolean, default=False)  # Gemini checks each clip, one redo if off
    old_drive_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # trashed once a redo is saved
    job_kind: Mapped[str] = mapped_column(String(20), default="")  # what worker_job_id is: "" clip | still | check
    provider: Mapped[str] = mapped_column(String(20), default="gemini")  # where the clips are made: gemini | flow
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    scenes: Mapped[list["Scene"]] = relationship(order_by="Scene.position", cascade="all, delete-orphan",
                                                 lazy="selectin")


class Scene(Base):
    """One ~10 s clip of a generation. The worker/poll fields on Generation track the scene in progress
    (the first one without a clip); finished clips are joined into one video once all are done."""

    __tablename__ = "scenes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation_id: Mapped[int] = mapped_column(ForeignKey("generations.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    prompt: Mapped[str] = mapped_column(Text)  # the clip prompt sent to Veo; "" until planned (and approved)
    clip_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # set once the clip is downloaded
    # The script (app/script.py): `prompt` is compiled from these when they are set.
    setting: Mapped[str] = mapped_column(Text, default="")
    time_of_day: Mapped[str] = mapped_column(String(60), default="")
    action: Mapped[str] = mapped_column(Text, default="")
    sound: Mapped[str] = mapped_column(Text, default="")
    shot: Mapped[dict] = mapped_column(JSON, default=dict)  # framing, angle, movement, lens
    characters: Mapped[list] = mapped_column(JSON, default=list)  # [{id, name, position}]
    dialogue: Mapped[list] = mapped_column(JSON, default=list)  # [{id, name, line}]
    still_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # its locked first frame
    check_note: Mapped[str] = mapped_column(Text, default="")  # what Gemini's clip check said
    redos: Mapped[int] = mapped_column(Integer, default=0)  # automatic remakes after a failed check

    SCRIPT_FIELDS = ("setting", "time_of_day", "action", "sound", "shot", "characters", "dialogue")

    def script(self) -> dict:
        return {k: getattr(self, k) or ({} if k == "shot" else [] if k in ("characters", "dialogue") else "")
                for k in self.SCRIPT_FIELDS}


class Character(Base):
    """A reusable cast member. Their words are copied into every clip they appear in."""

    __tablename__ = "characters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    appearance: Mapped[str] = mapped_column(Text, default="")  # face, hair, age, build
    outfit: Mapped[str] = mapped_column(Text, default="")
    voice: Mapped[str] = mapped_column(Text, default="")  # gender, age, accent, pitch, pace, tone
    personality: Mapped[str] = mapped_column(Text, default="")  # guides Gemini's script, not the clip prompt
    locked: Mapped[str] = mapped_column(Text, default="")  # traits that must never change, said in every clip
    never: Mapped[str] = mapped_column(Text, default="")  # what Veo tends to get wrong about them
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # under var/characters
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def image_url(self) -> str | None:
        """Changes with every new picture, so browsers can cache each one."""
        if not self.image_path:
            return None
        return f"/characters/{self.id}/image?v={self.image_path.rsplit('/', 1)[-1].split('.')[0]}"

    def sheet(self) -> dict:
        return {"id": self.id, "name": self.name, "appearance": self.appearance, "outfit": self.outfit,
                "voice": self.voice, "personality": self.personality, "locked": self.locked, "never": self.never,
                "image_path": self.image_path}


class Portrait(Base):
    """A character portrait Gemini drew from a description, before (or without) it becomes a character's picture."""

    __tablename__ = "portraits"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20))  # working | ready | failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # under var/portraits
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class GoogleConnection(Base):
    """At most one row per user: their Drive connection. Only the refresh token is stored, encrypted."""

    __tablename__ = "google_connection"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    refresh_token_enc: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    folder_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    folder_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
