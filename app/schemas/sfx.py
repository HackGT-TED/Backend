"""Sound-effect cues returned by xAI and consumed by the mixer."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


KIND_ONESHOT = "oneshot"
KIND_AMBIENT = "ambient"
KIND_FILL_PAUSE = "fill_pause"
CUE_KINDS = (KIND_ONESHOT, KIND_AMBIENT, KIND_FILL_PAUSE)
BED_KINDS = frozenset({KIND_AMBIENT, KIND_FILL_PAUSE})

# Whole-cue level. Ducking during overlap is applied later by the mixer.
GAIN_MIN_DB = -24.0
GAIN_MAX_DB = 6.0

_KIND_ALIASES = {
    "oneshot": KIND_ONESHOT,
    "one-shot": KIND_ONESHOT,
    "one_shot": KIND_ONESHOT,
    "ambient": KIND_AMBIENT,
    "ambience": KIND_AMBIENT,
    "bed": KIND_AMBIENT,
    "fill_pause": KIND_FILL_PAUSE,
    "fill-pause": KIND_FILL_PAUSE,
    "pause": KIND_FILL_PAUSE,
}

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
                        "description": "Exact catalog id. Empty only when query carries that same id.",
                    },
                    "query": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "Same catalog id, or null. Never a free-text search.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "One short sentence explaining the match and the window.",
                    },
                    "start": {
                        "type": "number",
                        "description": "Start time in seconds on the story clock.",
                    },
                    "end": {
                        "anyOf": [{"type": "number"}, {"type": "null"}],
                        "description": "End time in seconds. Null when until_seconds is set, or when an ambient bed holds to the end of the story.",
                    },
                    "kind": {
                        "anyOf": [
                            {"type": "string", "enum": list(CUE_KINDS)},
                            {"type": "null"},
                        ],
                        "description": "oneshot, ambient, or fill_pause. Null means oneshot.",
                    },
                    "gain_db": {
                        "anyOf": [{"type": "number"}, {"type": "null"}],
                        "description": "Whole-cue gain in decibels. Null is unity. Beds are often -6 to -12.",
                    },
                    "end_at_scene_change": {
                        "anyOf": [{"type": "boolean"}, {"type": "null"}],
                        "description": "True when the bed ends because the scene changed. True with end and until_seconds null holds the bed to the end of the story.",
                    },
                    "until_seconds": {
                        "anyOf": [{"type": "number"}, {"type": "null"}],
                        "description": "Alternate end clock in seconds. Null when end is set.",
                    },
                },
                "required": [
                    "catalog_id",
                    "query",
                    "reason",
                    "start",
                    "end",
                    "kind",
                    "gain_db",
                    "end_at_scene_change",
                    "until_seconds",
                ],
            },
        }
    },
    "required": ["cues"],
}

SFX_RESPONSE_FORMAT: dict = {
    "type": "json_schema",
    "json_schema": {
        "name": "sfx_plan",
        "description": "Layered sound cues for a children's story: oneshots, scene beds, and optional pause fills.",
        "strict": True,
        "schema": SFX_PLAN_SCHEMA,
    },
}

_MIN_WINDOW_MS = 50


class SfxCue(BaseModel):
    """One placed effect.

    xAI is asked for ``start`` and ``end`` in seconds. ``start_ms`` and
    ``end_ms`` are accepted as well so callers can pass millisecond windows.
    ``kind`` (also accepted as ``role``) is ``oneshot``, ``ambient``, or
    ``fill_pause``. An ambient bed may omit ``end`` and set ``until_seconds``,
    or set ``end_at_scene_change`` and hold through the rest of the story.
    """

    model_config = ConfigDict(extra="ignore")

    query: str | None = None
    catalog_id: str = ""
    description: str = ""
    reason: str = ""
    start: float | None = None
    end: float | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    kind: str | None = None
    role: str | None = None
    gain_db: float | None = None
    end_at_scene_change: bool | None = None
    until_seconds: float | None = None

    @model_validator(mode="after")
    def _normalize(self) -> "SfxCue":
        self.catalog_id = (self.catalog_id or "").strip()
        self.query = (self.query or "").strip()
        if self.catalog_id and not self.query:
            self.query = self.catalog_id
        elif self.query and not self.catalog_id:
            self.catalog_id = self.query
        if not self.catalog_id:
            raise ValueError("SFX cue is missing catalog_id")

        self.reason = self.reason.strip()
        self.description = self.description.strip()
        if self.reason and not self.description:
            self.description = self.reason
        elif self.description and not self.reason:
            self.reason = self.description

        raw_kind = (self.kind or self.role or KIND_ONESHOT).strip().lower()
        self.kind = _KIND_ALIASES.get(raw_kind, KIND_ONESHOT)
        self.role = self.kind

        if self.gain_db is not None:
            self.gain_db = max(GAIN_MIN_DB, min(GAIN_MAX_DB, float(self.gain_db)))
        return self

    def window_ms(self) -> tuple[int, int]:
        window = cue_window_ms(self)
        if window is None:
            raise ValueError(f"SFX cue {self.query!r} is missing start and end")
        return window


class SfxPlan(BaseModel):
    cues: list[SfxCue] = Field(default_factory=list)


def cue_window_ms(cue: SfxCue, duration_ms: int | None = None) -> tuple[int, int] | None:
    """Resolve a cue window.

    ``end`` wins, then ``until_seconds``, then ``end_ms``. An ambient or
    pause-fill cue with ``end_at_scene_change`` and no end holds to
    ``duration_ms``.
    """

    if cue.start is not None:
        start_ms = int(round(cue.start * 1000))
    elif cue.start_ms is not None:
        start_ms = int(cue.start_ms)
    else:
        return None

    if cue.end is not None:
        end_ms = int(round(cue.end * 1000))
    elif cue.until_seconds is not None:
        end_ms = int(round(cue.until_seconds * 1000))
    elif cue.end_ms is not None:
        end_ms = int(cue.end_ms)
    elif cue.end_at_scene_change and cue.kind in BED_KINDS and duration_ms:
        end_ms = int(duration_ms)
    else:
        return None
    return start_ms, end_ms


def align_cues(cues: list[SfxCue], duration_seconds: float) -> list[SfxCue]:
    """Clamp cue windows to the story timeline and drop windows that are too short.

    Overlapping cues are all kept. A rain bed and a door creak at the same
    moment both survive so the mixer can overlay them.
    """

    duration_ms = max(int(round(duration_seconds * 1000)), 0)
    aligned: list[SfxCue] = []
    for cue in cues:
        window = cue_window_ms(cue, duration_ms)
        if window is None:
            continue
        start_ms, end_ms = window
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
