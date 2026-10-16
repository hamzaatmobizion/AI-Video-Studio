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
    prompt: Mapped[str] = mapped_column(Text)  # "" until Gemini has planned the scenes
    clip_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # set once the clip is downloaded


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
