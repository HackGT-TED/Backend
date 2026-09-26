"""Build an SFX-only timeline and export it as MP3.

The timeline is silence of the story duration, with each clip overlaid at the
cue's start timestamp and trimmed so it cannot spill past the cue end or the
story end. Export uses pydub, which shells out to ffmpeg (libmp3lame). WAV
bytes are decoded in-process; MP3 and OGG previews need ffmpeg on ``PATH``.
"""

import io
from dataclasses import dataclass
from pathlib import Path

from pydub import AudioSegment

_FRAME_RATE = 44100
_MIN_WINDOW_MS = 50
_FADE_MS = 10


class AudioMixError(RuntimeError):
    """A clip could not be decoded or the MP3 export failed."""


@dataclass(frozen=True)
class TimedClip:
    """Audio already downloaded, placed on the story clock."""

    start_ms: int
    end_ms: int
    audio_bytes: bytes
    query: str = ""


def mix_sfx_mp3(clips: list[TimedClip], duration_ms: int, output_path: Path) -> Path:
    """Write an SFX-only MP3 whose length matches ``duration_ms``."""

    timeline = place_clips(clips, duration_ms)
    return export_mp3(timeline, output_path)


def place_clips(clips: list[TimedClip], duration_ms: int) -> AudioSegment:
    """Overlay clips on a silent timeline. Length is exactly ``duration_ms``."""

    duration_ms = max(int(duration_ms), 1)
    timeline = AudioSegment.silent(duration=duration_ms, frame_rate=_FRAME_RATE)
    for clip in clips:
        placed = _prepare_clip(clip, timeline, duration_ms)
        if placed is None:
            continue
        audio, start_ms = placed
        timeline = timeline.overlay(audio, position=start_ms)
    if len(timeline) > duration_ms:
        timeline = timeline[:duration_ms]
    elif len(timeline) < duration_ms:
        pad = AudioSegment.silent(
            duration=duration_ms - len(timeline),
            frame_rate=timeline.frame_rate,
        )
        timeline += pad
    return timeline


def export_mp3(timeline: AudioSegment, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    try:
        handle = timeline.export(str(output_path), format="mp3", bitrate="128k")
    except Exception as exc:
        raise AudioMixError(
            "Failed to export the SFX MP3. Install ffmpeg with libmp3lame and "
            "ensure the ffmpeg binary is on PATH."
        ) from exc
    finally:
        if handle is not None:
            handle.close()
    if not output_path.is_file():
        raise AudioMixError(f"ffmpeg did not write {output_path}")
    return output_path


def _prepare_clip(
    clip: TimedClip,
    timeline: AudioSegment,
    duration_ms: int,
) -> tuple[AudioSegment, int] | None:
    start_ms = max(0, int(clip.start_ms))
    end_ms = min(duration_ms, int(clip.end_ms))
    window = end_ms - start_ms
    if window < _MIN_WINDOW_MS or start_ms >= duration_ms:
        return None
    audio = _match_format(load_clip(clip.audio_bytes), timeline)[:window]
    if len(audio) <= 0:
        return None
    if len(audio) > _FADE_MS * 2:
        audio = audio.fade_in(_FADE_MS).fade_out(_FADE_MS)
    return audio, start_ms


def load_clip(audio_bytes: bytes) -> AudioSegment:
    if not audio_bytes:
        raise AudioMixError("Downloaded clip was empty")
    buffer = io.BytesIO(audio_bytes)
    try:
        if audio_bytes[:4] == b"RIFF":
            return AudioSegment.from_wav(buffer)
        if audio_bytes[:3] == b"ID3" or _looks_like_mp3(audio_bytes):
            return AudioSegment.from_file(buffer, format="mp3")
        if audio_bytes[:4] == b"OggS":
            return AudioSegment.from_file(buffer, format="ogg")
        return AudioSegment.from_file(buffer)
    except AudioMixError:
        raise
    except Exception as exc:
        raise AudioMixError(f"Could not decode SFX clip: {exc}") from exc


def _looks_like_mp3(audio_bytes: bytes) -> bool:
    if len(audio_bytes) < 2:
        return False
    return audio_bytes[0] == 0xFF and audio_bytes[1] in {0xFB, 0xF3, 0xF2, 0xFA}


def _match_format(clip: AudioSegment, timeline: AudioSegment) -> AudioSegment:
    if clip.frame_rate != timeline.frame_rate:
        clip = clip.set_frame_rate(timeline.frame_rate)
    if clip.channels != timeline.channels:
        clip = clip.set_channels(timeline.channels)
    if clip.sample_width != timeline.sample_width:
        clip = clip.set_sample_width(timeline.sample_width)
    return clip
