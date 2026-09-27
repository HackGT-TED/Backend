"""Run Deepgram JSON through cue planning, FreeSound, and the SFX mix.

Synchronous on purpose for v1: the request stays open until the MP3 is written.
"""

import logging
from dataclasses import dataclass

from app.schemas.deepgram import NormalizedTranscript
from app.schemas.sfx import SfxCue, limit_to_one_cue_per_sentence
from app.services.freesound import (
    FreeSoundClient,
    FreeSoundError,
    FreeSoundNotConfiguredError,
    FreeSoundRateLimitError,
)
from app.services.loudness import gain_for_entry, sfx_level_db
from app.services.mixer import (
    AudioMixError,
    TimedClip,
    load_clip,
    mix_on_story_bytes,
    mix_sfx_bytes,
    story_duration_ms,
)
from app.services.sfx_catalog import CATALOG_PATH, catalog_choices, load_catalog
from app.services.xai import XaiClient

logger = logging.getLogger(__name__)


@dataclass
class PipelineOutput:
    duration_seconds: float
    cues: list[SfxCue]
    audio_bytes: bytes
    warnings: list[str]


def run_pipeline(
    transcript: NormalizedTranscript,
    xai: XaiClient,
    freesound: FreeSoundClient,
    story_bytes: bytes | None = None,
    catalog: dict | None = None,
) -> PipelineOutput:
    """Ask xAI which catalog sounds fit, fetch those clips, and mix them on the story clock.

    When ``story_bytes`` is the uploaded recording, its decoded length is the
    clock (word times stay where the transcriber put them) and the effects are
    mixed onto that audio. Without it, the result is an effects-only track the
    length of ``transcript.duration_seconds``.

    ``catalog`` is the runtime document (the Supabase row, or the checked-in
    file). It is what the planner sees, and it is bound on the downloader for
    this call so a preview comes from that same document.
    """

    token = None
    bind = getattr(freesound, "bind_catalog", None)
    if catalog is not None and callable(bind):
        token = bind(catalog)
    try:
        return _plan_and_mix(transcript, xai, freesound, story_bytes, catalog)
    finally:
        reset = getattr(freesound, "reset_catalog", None)
        if token is not None and callable(reset):
            reset(token)


def _plan_and_mix(
    transcript: NormalizedTranscript,
    xai: XaiClient,
    freesound: FreeSoundClient,
    story_bytes: bytes | None,
    catalog: dict | None,
) -> PipelineOutput:
    story_ms: int | None = None
    if story_bytes is not None:
        story_ms = story_duration_ms(story_bytes)
        transcript = transcript.model_copy(update={"duration_seconds": story_ms / 1000})

    choices = _catalog_for_planning(catalog)
    allowed = {item["id"] for item in choices}
    planned = xai.plan_cues(transcript, choices)
    cues = limit_to_one_cue_per_sentence(planned, transcript.sentence_windows())
    warnings: list[str] = []
    if len(cues) < len(planned):
        warnings.append(
            f"Dropped {len(planned) - len(cues)} cue(s) so each sentence has at most one sound effect"
        )
    timed: list[TimedClip] = []
    rate_limited = 0

    for cue in cues:
        start_ms, end_ms = cue.window_ms()
        if end_ms - start_ms < 50:
            warnings.append(f"Skipped cue {cue.catalog_id!r}: window is shorter than 50ms")
            continue
        if allowed and cue.catalog_id not in allowed:
            warnings.append(f"Skipped cue {cue.catalog_id!r}: not in the SFX catalog")
            logger.warning("Skipping cue %s: not in catalog", cue.catalog_id)
            continue
        try:
            downloaded = freesound.download_for_query(cue.catalog_id)
        except FreeSoundNotConfiguredError:
            raise
        except FreeSoundRateLimitError as exc:
            rate_limited += 1
            warnings.append(f"Skipped cue {cue.query!r}: {exc}")
            logger.warning("FreeSound rate limit for %s", cue.query)
            continue
        except FreeSoundError as exc:
            warnings.append(f"Skipped cue {cue.query!r}: {exc}")
            logger.warning("Skipping cue %s: %s", cue.query, exc)
            continue

        try:
            decoded = load_clip(downloaded.audio_bytes)
        except AudioMixError as exc:
            warnings.append(f"Skipped cue {cue.query!r}: {exc}")
            logger.warning("Undecodable clip for %s: %s", cue.query, exc)
            continue

        document = _catalog_document(catalog)
        entry = _catalog_entry(document, cue.catalog_id)
        match = gain_for_entry(entry, decoded)
        timed.append(
            TimedClip(
                start_ms=start_ms,
                end_ms=end_ms,
                audio_bytes=downloaded.audio_bytes,
                query=cue.query,
                gain_db=round(match + sfx_level_db(document), 1),
                trim_start_ms=trim_start_ms(entry),
            )
        )

    if cues and rate_limited == len(cues) and not timed:
        raise FreeSoundRateLimitError(
            "FreeSound rate limit exceeded for every sound-effect cue"
        )

    if story_ms is not None:
        duration_ms = story_ms
        audio_bytes = mix_on_story_bytes(story_bytes or b"", timed)
    else:
        duration_ms = max(int(round(transcript.duration_seconds * 1000)), 1)
        audio_bytes = mix_sfx_bytes(timed, duration_ms)
    return PipelineOutput(
        duration_seconds=duration_ms / 1000,
        cues=cues,
        audio_bytes=audio_bytes,
        warnings=warnings,
    )


def _catalog_for_planning(catalog: dict | None = None) -> list[dict]:
    document = _catalog_document(catalog)
    if document is None:
        return []
    entries = document.get("entries")
    if not isinstance(entries, list):
        return []
    return catalog_choices(entries)


def trim_start_ms(entry: dict | None) -> int:
    """The entry's ``trim_start_s`` (seconds of lead-in to skip) in ms. Missing or invalid is 0."""

    value = entry.get("trim_start_s") if isinstance(entry, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        return 0
    return int(round(value * 1000))


def _catalog_entry(catalog: dict | None, slot_id: str) -> dict | None:
    document = _catalog_document(catalog)
    if document is None:
        return None
    entries = document.get("entries")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("id") or "") == slot_id:
            return entry
    return None


def _catalog_document(catalog: dict | None) -> dict | None:
    if catalog is None:
        try:
            catalog = load_catalog(CATALOG_PATH)
        except (OSError, ValueError) as exc:
            logger.warning("SFX catalog could not be loaded for planning: %s", exc)
            return None
    if not isinstance(catalog, dict):
        return None
    return catalog
