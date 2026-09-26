"""HTTP request and response models for story recordings."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProcessStoryRequest(BaseModel):
    """Deepgram JSON plus optional story metadata.

    Send the Deepgram document under ``deepgram``, or post the Deepgram JSON
    itself at the top level (a body that already has ``results`` or ``transcript``).
    """

    model_config = ConfigDict(extra="allow")

    story_id: str | None = None
    title: str | None = None
    narrator: str | None = None
    source_audio_url: str | None = None
    deepgram: dict[str, Any] | None = None

    def deepgram_payload(self) -> dict[str, Any]:
        if self.deepgram is not None:
            return self.deepgram
        extra = dict(self.model_extra or {})
        if "results" in extra or "transcript" in extra or "words" in extra:
            return extra
        raise ValueError("Request must include a deepgram object or a Deepgram JSON body")


class CueOut(BaseModel):
    query: str
    description: str = ""
    start: float
    end: float
    start_ms: int
    end_ms: int


class RecordingSummary(BaseModel):
    id: str
    story_id: str | None = None
    title: str | None = None
    narrator: str | None = None
    status: str
    duration_seconds: float
    sfx_url: str | None = None
    created_at: datetime


class RecordingDetail(RecordingSummary):
    source_audio_url: str | None = None
    transcript_text: str = ""
    cues: list[CueOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error_message: str | None = None
    transcript_json: dict[str, Any] = Field(default_factory=dict)
