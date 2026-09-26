"""Run Deepgram JSON through cue planning, FreeSound, and the SFX mix.

Synchronous on purpose for v1: the request stays open until the MP3 is written.
"""

import logging
from dataclasses import dataclass

from app.schemas.deepgram import NormalizedTranscript
from app.schemas.sfx import SfxCue
from app.services.freesound import (
    FreeSoundClient,
    FreeSoundError,
    FreeSoundNotConfiguredError,
    FreeSoundRateLimitError,
)
from app.services.mixer import AudioMixError, TimedClip, load_clip, mix_sfx_bytes
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
    catalog: dict | None = None,
) -> PipelineOutput:
    """Ask xAI which catalog sounds fit, fetch those clips, and mix them on the story clock.

    ``catalog`` is the active library (Supabase, or the checked-in JSON). When
    it is omitted, the checked-in file is used. The same document is bound onto
    ``freesound`` so downloads use those preview URLs.
    """

    token = None
    if catalog is not None:
        bind = getattr(freesound, "bind_catalog", None)
        if callable(bind):
            token = bind(catalog)
    try:
        return _mix(transcript, xai, freesound, catalog)
    finally:
        reset = getattr(freesound, "reset_catalog", None)
        if token is not None and callable(reset):
            reset(token)


def _mix(
    transcript: NormalizedTranscript,
    xai: XaiClient,
    freesound: FreeSoundClient,
    catalog: dict | None,
) -> PipelineOutput:
    choices = _catalog_for_planning(catalog)
    allowed = {item["id"] for item in choices}
    cues = xai.plan_cues(transcript, choices)
    warnings: list[str] = []
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
            load_clip(downloaded.audio_bytes)
        except AudioMixError as exc:
            warnings.append(f"Skipped cue {cue.query!r}: {exc}")
            logger.warning("Undecodable clip for %s: %s", cue.query, exc)
            continue

        timed.append(
            TimedClip(
                start_ms=start_ms,
                end_ms=end_ms,
                audio_bytes=downloaded.audio_bytes,
                query=cue.query,
            )
        )

    if cues and rate_limited == len(cues) and not timed:
        raise FreeSoundRateLimitError(
            "FreeSound rate limit exceeded for every sound-effect cue"
        )

    duration_ms = max(int(round(transcript.duration_seconds * 1000)), 1)
    audio_bytes = mix_sfx_bytes(timed, duration_ms)
    return PipelineOutput(
        duration_seconds=duration_ms / 1000,
        cues=cues,
        audio_bytes=audio_bytes,
        warnings=warnings,
    )


def _catalog_for_planning(catalog: dict | None = None) -> list[dict]:
    if catalog is None:
        try:
            catalog = load_catalog(CATALOG_PATH)
        except (OSError, ValueError) as exc:
            logger.warning("SFX catalog could not be loaded for planning: %s", exc)
            return []
    if not isinstance(catalog, dict):
        return []
    entries = catalog.get("entries")
    if not isinstance(entries, list):
        return []
    return catalog_choices(entries)
