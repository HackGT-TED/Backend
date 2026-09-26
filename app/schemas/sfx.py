"""Sound-effect cues returned by Muse Spark and consumed by the mixer."""

from pydantic import BaseModel, ConfigDict, Field


SFX_PLAN_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "cues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Short FreeSound search query, two to six words.",
                    },
                    "description": {
                        "type": "string",
                        "description": "One sentence explaining why this effect fits the story.",
                    },
                    "start": {
                        "type": "number",
                        "description": "Start time in seconds, aligned to the transcript.",
                    },
                    "end": {
                        "type": "number",
                        "description": "End time in seconds, aligned to the transcript.",
                    },
                },
                "required": ["query", "description", "start", "end"],
            },
        }
    },
    "required": ["cues"],
}

SFX_RESPONSE_FORMAT: dict = {
    "type": "json_schema",
    "json_schema": {
        "name": "sfx_plan",
        "description": "Timed sound-effect cues for a children's story.",
        "strict": True,
        "schema": SFX_PLAN_SCHEMA,
    },
}

_MIN_WINDOW_MS = 50


class SfxCue(BaseModel):
    """One placed effect.

    Muse Spark is asked for ``start`` and ``end`` in seconds. ``start_ms`` and
    ``end_ms`` are accepted as well so callers can pass millisecond windows.
    """

    model_config = ConfigDict(extra="ignore")

    query: str
    description: str = ""
    start: float | None = None
    end: float | None = None
    start_ms: int | None = None
    end_ms: int | None = None

    def window_ms(self) -> tuple[int, int]:
        if self.start_ms is not None and self.end_ms is not None:
            return int(self.start_ms), int(self.end_ms)
        if self.start is None or self.end is None:
            raise ValueError(f"SFX cue {self.query!r} is missing start and end")
        return int(round(self.start * 1000)), int(round(self.end * 1000))


class SfxPlan(BaseModel):
    cues: list[SfxCue] = Field(default_factory=list)


def align_cues(cues: list[SfxCue], duration_seconds: float) -> list[SfxCue]:
    """Clamp cue windows to the story timeline and drop windows that are too short."""

    duration_ms = max(int(round(duration_seconds * 1000)), 0)
    aligned: list[SfxCue] = []
    for cue in cues:
        try:
            start_ms, end_ms = cue.window_ms()
        except ValueError:
            continue
        start_ms = max(0, start_ms)
        if duration_ms:
            end_ms = min(duration_ms, end_ms)
        if end_ms - start_ms < _MIN_WINDOW_MS:
            continue
        aligned.append(
            cue.model_copy(
                update={
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "start": start_ms / 1000,
                    "end": end_ms / 1000,
                }
            )
        )
    return aligned
