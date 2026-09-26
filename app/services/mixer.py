"""Build an SFX-only timeline and export it as MP3.

Mixing writes a temporary file (the system temp directory, ``/tmp`` on Vercel)
and returns the bytes. Callers upload those bytes; nothing is kept on local disk.

The timeline is silence of the story duration. Every cue is overlaid at its
own start, including cues that share a timestamp: a rain bed and a door creak
are both mixed, not replaced. One-shots are trimmed to their window. Ambient
and pause-fill beds are looped so a short preview lasts the whole window.
Those beds are ducked by ``ONESHOT_DUCK_DB`` while a one-shot overlaps them.
``gain_db`` on a clip, when set, is applied for the whole window before that
duck. Export uses pydub, which shells out to ffmpeg (libmp3lame). WAV bytes
are decoded in-process. MP3 and OGG previews are decoded with ffmpeg. A
system ``ffmpeg`` on ``PATH`` is used when present. Otherwise the
``imageio-ffmpeg`` binary is used so a Vercel function can mix without a
system package.
"""

import io
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydub import AudioSegment

from app.schemas.sfx import BED_KINDS

_FRAME_RATE = 44100
_MIN_WINDOW_MS = 50
_FADE_MS = 10
# Extra attenuation applied to a bed only where a one-shot is actually sounding.
ONESHOT_DUCK_DB = -6.0
_LOOP_CROSSFADE_MS = 40


class AudioMixError(RuntimeError):
    """A clip could not be decoded or the MP3 export failed."""


@dataclass(frozen=True)
class TimedClip:
    """Audio already downloaded, placed on the story clock.

    ``kind`` is ``oneshot``, ``ambient``, or ``fill_pause``. Beds loop to fill
    ``end_ms - start_ms``. ``gain_db`` is an optional whole-clip level.
    """

    start_ms: int
    end_ms: int
    audio_bytes: bytes
    query: str = ""
    kind: str = "oneshot"
    gain_db: float | None = None


def mix_sfx_bytes(clips: list[TimedClip], duration_ms: int) -> bytes:
    """Mix clips and return the MP3 bytes. The temp file is removed before return."""

    with tempfile.TemporaryDirectory(prefix="story-sfx-") as directory:
        path = Path(directory) / "sfx.mp3"
        mix_sfx_mp3(clips, duration_ms, path)
        return path.read_bytes()


def mix_sfx_mp3(clips: list[TimedClip], duration_ms: int, output_path: Path) -> Path:
    """Write an SFX-only MP3 whose length matches ``duration_ms``."""

    timeline = place_clips(clips, duration_ms)
    return export_mp3(timeline, output_path)


def place_clips(clips: list[TimedClip], duration_ms: int) -> AudioSegment:
    """Overlay every clip on a silent timeline. Length is exactly ``duration_ms``.

    Clips that occupy the same time are all mixed. Nothing is dropped because
    another cue already covers that timestamp. Scene beds duck under one-shots.
    """

    duration_ms = max(int(duration_ms), 1)
    timeline = AudioSegment.silent(duration=duration_ms, frame_rate=_FRAME_RATE)
    prepared: list[tuple[AudioSegment, int, TimedClip]] = []
    for clip in clips:
        placed = _prepare_clip(clip, timeline, duration_ms)
        if placed is None:
            continue
        audio, start_ms = placed
        prepared.append((audio, start_ms, clip))

    oneshot_spans = [
        (start_ms, start_ms + len(audio))
        for audio, start_ms, clip in prepared
        if not _is_bed(clip)
    ]
    for audio, start_ms, clip in prepared:
        if _is_bed(clip) and oneshot_spans:
            audio = _duck_under_oneshots(audio, start_ms, oneshot_spans)
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
    AudioSegment.converter = ffmpeg_exe()
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
    audio = _match_format(load_clip(clip.audio_bytes), timeline)
    if _is_bed(clip):
        audio = _loop_to_window(audio, window)
    else:
        audio = audio[:window]
    if len(audio) <= 0:
        return None
    if clip.gain_db:
        audio = audio.apply_gain(clip.gain_db)
    if len(audio) > _FADE_MS * 2:
        audio = audio.fade_in(_FADE_MS).fade_out(_FADE_MS)
    return audio, start_ms


def _is_bed(clip: TimedClip) -> bool:
    return clip.kind in BED_KINDS


def _loop_to_window(audio: AudioSegment, window_ms: int) -> AudioSegment:
    """Repeat a short bed so it fills ``window_ms``. Longer sources are trimmed."""

    if len(audio) >= window_ms:
        return audio[:window_ms]
    if len(audio) <= 0:
        return audio
    crossfade = min(_LOOP_CROSSFADE_MS, len(audio) // 4)
    if crossfade < 8:
        looped = audio
        while len(looped) < window_ms:
            looped += audio
        return looped[:window_ms]

    looped = audio
    step = max(len(audio) - crossfade, 1)
    repeats = max((window_ms - len(looped) + step - 1) // step, 0)
    for _ in range(repeats):
        fade = min(crossfade, len(looped) // 2, len(audio) // 2)
        if fade < 8:
            looped += audio
        else:
            looped = looped.append(audio, crossfade=fade)
    if len(looped) < window_ms:
        looped += audio
    return looped[:window_ms]


def _duck_under_oneshots(
    audio: AudioSegment,
    start_ms: int,
    oneshot_spans: list[tuple[int, int]],
) -> AudioSegment:
    """Lower a bed by ``ONESHOT_DUCK_DB`` where a one-shot is sounding."""

    bed_end = start_ms + len(audio)
    relative: list[tuple[int, int]] = []
    for shot_start, shot_end in oneshot_spans:
        overlap_start = max(start_ms, shot_start)
        overlap_end = min(bed_end, shot_end)
        if overlap_end - overlap_start < 1:
            continue
        relative.append((overlap_start - start_ms, overlap_end - start_ms))
    for span_start, span_end in _merge_spans(relative):
        audio = _attenuate_span(audio, span_start, span_end, ONESHOT_DUCK_DB)
    return audio


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _attenuate_span(
    audio: AudioSegment,
    start_ms: int,
    end_ms: int,
    gain_db: float,
) -> AudioSegment:
    start_ms = max(0, min(len(audio), int(start_ms)))
    end_ms = max(start_ms, min(len(audio), int(end_ms)))
    if end_ms - start_ms < 1 or gain_db == 0:
        return audio
    quieter = audio[start_ms:end_ms].apply_gain(gain_db)
    rebuilt = audio[:start_ms] + quieter + audio[end_ms:]
    if len(rebuilt) > len(audio):
        return rebuilt[: len(audio)]
    if len(rebuilt) < len(audio):
        rebuilt += AudioSegment.silent(
            duration=len(audio) - len(rebuilt),
            frame_rate=audio.frame_rate,
        )
    return rebuilt


def load_clip(audio_bytes: bytes) -> AudioSegment:
    if not audio_bytes:
        raise AudioMixError("Downloaded clip was empty")
    try:
        if audio_bytes[:4] == b"RIFF":
            return AudioSegment.from_wav(io.BytesIO(audio_bytes))
        wav = _transcode_to_wav(audio_bytes)
        return AudioSegment.from_wav(io.BytesIO(wav))
    except AudioMixError:
        raise
    except Exception as exc:
        raise AudioMixError(f"Could not decode SFX clip: {exc}") from exc


def ffmpeg_exe() -> str:
    """Return a usable ffmpeg binary, preferring one already on ``PATH``."""

    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise AudioMixError(
            "ffmpeg is not on PATH. Install ffmpeg locally, or install project "
            "dependencies so the imageio-ffmpeg binary can be used on Vercel."
        ) from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


def _transcode_to_wav(audio_bytes: bytes) -> bytes:
    proc = subprocess.run(
        [
            ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-f",
            "wav",
            "pipe:1",
        ],
        input=audio_bytes,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.startswith(b"RIFF"):
        detail = proc.stderr.decode("utf-8", errors="replace")[:300]
        raise AudioMixError(f"ffmpeg could not decode the SFX clip: {detail}")
    return proc.stdout


def _match_format(clip: AudioSegment, timeline: AudioSegment) -> AudioSegment:
    if clip.frame_rate != timeline.frame_rate:
        clip = clip.set_frame_rate(timeline.frame_rate)
    if clip.channels != timeline.channels:
        clip = clip.set_channels(timeline.channels)
    if clip.sample_width != timeline.sample_width:
        clip = clip.set_sample_width(timeline.sample_width)
    return clip
