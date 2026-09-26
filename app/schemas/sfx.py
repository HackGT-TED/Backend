"""Sound-effect cues returned by xAI and consumed by the mixer."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
                    "catalog_id": {
                        "type": "string",
                        "description": "Exact id copied from the catalog list.",
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
                "required": ["catalog_id", "description", "start", "end"],
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

    xAI is asked for ``start`` and ``end`` in seconds. ``start_ms`` and
    ``end_ms`` are accepted as well so callers can pass millisecond windows.
    """

    model_config = ConfigDict(extra="ignore")

    query: str = ""
    catalog_id: str = ""
    description: str = ""
    start: float | None = None
    end: float | None = None
    start_ms: int | None = None
    end_ms: int | None = None

    @model_validator(mode="after")
    def _sync_catalog_id(self) -> "SfxCue":
        if self.catalog_id and not self.query:
            self.query = self.catalog_id
        elif self.query and not self.catalog_id:
            self.catalog_id = self.query
        if not self.catalog_id:
            raise ValueError("SFX cue is missing catalog_id")
        return self

    def window_ms(self) -> tuple[int, int]:
        if self.start_ms is not None and self.end_ms is not None:
            return int(self.start_ms), int(self.end_ms)
        if self.start is None or self.end is None:
            raise ValueError(f"SFX cue {self.query!r} is missing start and end")
        return int(round(self.start * 1000)), int(round(self.end * 1000))


class SfxPlan(BaseModel):
    cues: list[SfxCue] = Field(default_factory=list)


def limit_to_one_cue_per_sentence(
    cues: list[SfxCue],
    windows: list[tuple[float, float]],
) -> list[SfxCue]:
    """Keep the earliest cue in each sentence window.

    ``windows`` are ``(start_seconds, end_seconds)`` for each sentence. A cue
    that falls between sentences counts toward the nearest one. When no
    sentence times are available, only the earliest cue is kept.
    """

    usable: list[tuple[int, SfxCue]] = []
    for cue in cues:
        try:
            start_ms, _end_ms = cue.window_ms()
        except ValueError:
            continue
        usable.append((start_ms, cue))
    usable.sort(key=lambda item: item[0])
    if not usable:
        return []
    if len(usable) == 1:
        return [usable[0][1]]
    if not windows:
        return [usable[0][1]]

    used: set[int] = set()
    kept: list[SfxCue] = []
    for start_ms, cue in usable:
        index = _window_index(start_ms / 1000, windows)
        if index in used:
            continue
        used.add(index)
        kept.append(cue)
    return kept


def _window_index(start_s: float, windows: list[tuple[float, float]]) -> int:
    for index, (begin, end) in enumerate(windows):
        if begin <= start_s <= end:
            return index
    best = 0
    best_dist = float("inf")
    for index, (begin, end) in enumerate(windows):
        dist = begin - start_s if start_s < begin else start_s - end
        if dist < best_dist:
            best_dist = dist
            best = index
    return best


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
