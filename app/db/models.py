"""Recording and StoryAudio tables.

``create_all`` runs at startup (see ``app.db.session``). There is no Alembic
migration chain in v1; delete the local SQLite file if the schema changes.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Recording(Base):
    """One grandparent story and the transcript used to score it."""

    __tablename__ = "recordings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    story_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    narrator: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_audio_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    transcript_text: Mapped[str] = mapped_column(Text, default="")
    transcript_json: Mapped[dict] = mapped_column(JSON, default=dict)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(32), default="processing", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    story_audio: Mapped["StoryAudio | None"] = relationship(
        back_populates="recording",
        cascade="all, delete-orphan",
        uselist=False,
    )


class StoryAudio(Base):
    """The SFX-only MP3 mixed for a recording, plus the cues that placed it."""

    __tablename__ = "story_audio"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recording_id: Mapped[str] = mapped_column(
        ForeignKey("recordings.id"),
        unique=True,
        index=True,
    )
    sfx_mp3_path: Mapped[str] = mapped_column(String(512))
    cues_json: Mapped[list] = mapped_column(JSON, default=list)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    recording: Mapped[Recording] = relationship(back_populates="story_audio")
