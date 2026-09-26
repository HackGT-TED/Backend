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
) -> PipelineOutput:
    """Ask xAI which catalog sounds fit, fetch those clips, and mix them on the story clock.

    When ``story_bytes`` is the uploaded recording, its decoded length is the
    clock (word times stay where the transcriber put them) and the effects are
    mixed onto that audio. Without it, the result is an effects-only track the
    length of ``transcript.duration_seconds``.
    """

    story_ms: int | None = None
    if story_bytes is not None:
        story_ms = story_duration_ms(story_bytes)
        transcript = transcript.model_copy(update={"duration_seconds": story_ms / 1000})

    choices = _catalog_for_planning()
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
