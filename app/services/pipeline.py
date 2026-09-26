"""Run Deepgram JSON through cue planning, FreeSound, and the SFX mix.

Synchronous on purpose for v1: the request stays open until the MP3 is written.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from app.schemas.deepgram import NormalizedTranscript
from app.schemas.sfx import SfxCue
from app.services.freesound import (
    FreeSoundClient,
    FreeSoundError,
    FreeSoundNotConfiguredError,
    FreeSoundRateLimitError,
)
from app.services.mixer import AudioMixError, TimedClip, load_clip, mix_sfx_mp3
from app.services.muse_spark import MuseSparkClient

logger = logging.getLogger(__name__)


@dataclass
class PipelineOutput:
    duration_seconds: float
    cues: list[SfxCue]
    sfx_path: Path
    relative_path: str
    warnings: list[str]


def run_pipeline(
    transcript: NormalizedTranscript,
    muse: MuseSparkClient,
    freesound: FreeSoundClient,
    media_dir: Path,
    recording_id: str,
) -> PipelineOutput:
    """Plan cues, download previews, and write ``{recording_id}/sfx.mp3``."""

    cues = muse.plan_cues(transcript)
    warnings: list[str] = []
    timed: list[TimedClip] = []
    rate_limited = 0

    for cue in cues:
        start_ms, end_ms = cue.window_ms()
        if end_ms - start_ms < 50:
            warnings.append(f"Skipped cue {cue.query!r}: window is shorter than 50ms")
            continue
        try:
            downloaded = freesound.download_for_query(cue.query)
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
    relative_path = f"{recording_id}/sfx.mp3"
    sfx_path = media_dir / relative_path
    mix_sfx_mp3(timed, duration_ms, sfx_path)
    return PipelineOutput(
        duration_seconds=duration_ms / 1000,
        cues=cues,
        sfx_path=sfx_path,
        relative_path=relative_path,
        warnings=warnings,
    )
