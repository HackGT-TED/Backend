"""Place catalog clips on a story clock and export MP3.

Mixing writes a temporary file (the system temp directory, ``/tmp`` on Vercel)
and returns the bytes. Callers upload those bytes; nothing is kept on local disk.

``mix_sfx_bytes`` builds silence the length of the story and overlays each clip.
``mix_on_story_bytes`` overlays the same clips on the uploaded recording. Each
effect is delayed slightly past the word start and played at its catalog gain
plus the shared ``sfx_level_db``. On a recording, every effect is then turned
down to sit ``SFX_UNDER_VOICE_LU`` below the narration's measured loudness,
and while it plays it follows the narration under it (quieter while the
narrator whispers, louder while they speak up). A clip cannot spill past the
delayed cue end or the story end.

Export uses pydub, which shells out to ffmpeg (libmp3lame). WAV bytes are
decoded in-process. MP3 and OGG previews are decoded with ffmpeg. A system
``ffmpeg`` on ``PATH`` is used when present. Otherwise the ``imageio-ffmpeg``
binary is used so a Vercel function can mix without a system package.
"""

import io
import json
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydub import AudioSegment

try:
    import audioop
except ImportError:  # Python 3.13+ without audioop-lts
    from pydub import pyaudioop as audioop

_FRAME_RATE = 44100
_MIN_WINDOW_MS = 50
_FADE_MS = 10
# Word timestamps are the start of the word. A one-shot that begins there
# (a bark, a creak) is heard before the word is said.
SFX_START_DELAY_MS = 150
# How far below the narration's integrated loudness every effect sits (LU).
SFX_UNDER_VOICE_LU = 14.0
# Never boost past the catalog level, and never duck an effect into nothing.
_SFX_MIN_GAIN_DB = -40.0

# Loudness of the finished story (voice + effects). Podcasts and phone audio
# sit near -16 LUFS; quiet phone recordings come in around -30 to -40.
OUTPUT_TARGET_LUFS = -16.0
_OUTPUT_TRUE_PEAK_DBTP = -1.5
_OUTPUT_LRA = 11.0

# Following the voice over time: measure it every _ENV_FRAME_MS, smooth over
# _ENV_SMOOTH_MS so effects track phrases rather than syllables, and rescale
# the effect every _FOLLOW_STEP_MS while it plays.
_ENV_FRAME_MS = 50
_ENV_SMOOTH_MS = 400
_ENV_HOLD_MS = 1000
_FOLLOW_STEP_MS = 10
# Frames quieter than this far below the loud speech (or than the absolute
# floor) are pauses. Pauses hold the last spoken level, so effects don't drop
# out between sentences. Whispers stay well above this line.
_ENV_PAUSE_BELOW_LOUD_DB = 30.0
_ENV_SILENCE_DBFS = -60.0
# How far the voice-following may move an effect from its average level.
_FOLLOW_MIN_DB = -15.0
_FOLLOW_MAX_DB = 8.0


class AudioMixError(RuntimeError):
    """A clip could not be decoded or the MP3 export failed."""


class AudioJoinError(AudioMixError):
    """One of the recordings being joined could not be decoded."""

    def __init__(self, index: int, detail: str) -> None:
        super().__init__(f"Audio file {index} could not be decoded: {detail}")
        self.index = index


# Pause placed between story moments when several recordings are joined.
MOMENT_GAP_MS = 300


@dataclass(frozen=True)
class TimedClip:
    """Audio already downloaded, placed on the story clock."""

    start_ms: int
    end_ms: int
    audio_bytes: bytes
    query: str = ""
    # Catalog offset that puts this preview on the shared loudness. 0 leaves the file as fetched.
    gain_db: float = 0.0
    # Skip this much of the preview's start (lead-in before the sound is heard).
    trim_start_ms: int = 0


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


def mix_on_story_bytes(story_bytes: bytes, clips: list[TimedClip]) -> bytes:
    """Overlay ducked effects on the recording and return MP3 bytes."""

    with tempfile.TemporaryDirectory(prefix="story-sfx-") as directory:
        path = Path(directory) / "story.mp3"
        mix_on_story_mp3(story_bytes, clips, path)
        return path.read_bytes()


def mix_on_story_mp3(story_bytes: bytes, clips: list[TimedClip], output_path: Path) -> Path:
    """Write the recording with effects at each cue's start time, normalized to ``OUTPUT_TARGET_LUFS``."""

    return export_loud_mp3(overlay_on_story(story_bytes, clips), output_path)


def join_audio(parts: list[bytes]) -> bytes:
    """Join recordings in order, with ``MOMENT_GAP_MS`` of silence between them.

    Each part may be any format ffmpeg reads (m4a, webm, ogg, mp3, wav). Returns
    MP3 bytes, so the joined story goes through the same path as one upload.
    """

    joined: AudioSegment | None = None
    for index, audio_bytes in enumerate(parts, start=1):
        try:
            moment = load_clip(audio_bytes)
        except AudioMixError as exc:
            raise AudioJoinError(index, str(exc)) from exc
        if joined is None:
            joined = moment
            continue
        gap = _match_format(AudioSegment.silent(duration=MOMENT_GAP_MS), joined)
        joined = joined + gap + _match_format(moment, joined)
    if joined is None:
        raise AudioMixError("No audio to join")
    with tempfile.TemporaryDirectory(prefix="story-join-") as directory:
        path = Path(directory) / "story.mp3"
        export_mp3(joined, path)
        return path.read_bytes()


def story_duration_ms(story_bytes: bytes) -> int:
    """Decoded length of the uploaded recording. This is the mix clock."""

    return max(len(load_clip(story_bytes)), 1)


def place_clips(clips: list[TimedClip], duration_ms: int) -> AudioSegment:
    """Overlay clips on a silent timeline. Length is exactly ``duration_ms``."""

    duration_ms = max(int(duration_ms), 1)
    timeline = AudioSegment.silent(duration=duration_ms, frame_rate=_FRAME_RATE)
    return _overlay_clips(timeline, clips, duration_ms)


def overlay_on_story(story_bytes: bytes, clips: list[TimedClip]) -> AudioSegment:
    """Place clips on the decoded recording. Length matches that recording.

    Each clip carries the catalog match gain plus ``sfx_level_db``, which puts
    it on the shared catalog loudness. On top of that, every effect is turned
    down so it sits ``SFX_UNDER_VOICE_LU`` below this recording's narration on
    average, and then rises and falls with the narration under it: whispering
    lowers the effect, talking louder raises it.
    """

    # loudness imports this module, so import it here.
    from app.services.loudness import TARGET_LUFS, integrated_lufs

    story = load_clip(story_bytes)
    duration_ms = max(len(story), 1)
    duck_db = under_voice_gain_db(integrated_lufs(story_bytes), TARGET_LUFS)
    envelope = narration_envelope_db(story)
    return _overlay_clips(story, clips, duration_ms, extra_gain_db=duck_db, envelope=envelope)


def narration_envelope_db(story: AudioSegment) -> list[float]:
    """Per-frame narration level, in dB relative to the story's typical (median) speech level.

    One value per ``_ENV_FRAME_MS``. Pauses hold the last spoken level. The
    curve is smoothed and clamped to ``_FOLLOW_MIN_DB``..``_FOLLOW_MAX_DB``.
    """

    levels = [story[i : i + _ENV_FRAME_MS].dBFS for i in range(0, len(story), _ENV_FRAME_MS)]
    heard = sorted(level for level in levels if level != float("-inf"))
    if not heard:
        return [0.0] * len(levels)
    loud = heard[int(0.95 * (len(heard) - 1))]
    gate = max(_ENV_SILENCE_DBFS, loud - _ENV_PAUSE_BELOW_LOUD_DB)
    is_speech = [level >= gate for level in levels]
    if not any(is_speech):
        return [0.0] * len(levels)

    # Spoken frames: power average of the spoken frames around them. Pauses:
    # the power average of the last second of speech, so a pause holds the
    # phrase level rather than the fading tail of its last word.
    half = max(1, _ENV_SMOOTH_MS // _ENV_FRAME_MS // 2)
    lookback = max(1, _ENV_HOLD_MS // _ENV_FRAME_MS)
    power_sum, count = [0.0], [0]
    for level, spoken in zip(levels, is_speech):
        power_sum.append(power_sum[-1] + (_db_to_power(level) if spoken else 0.0))
        count.append(count[-1] + spoken)

    def spoken_average(lo: int, hi: int) -> float | None:
        n = count[hi] - count[lo]
        return _power_to_db((power_sum[hi] - power_sum[lo]) / n) if n else None

    held: list[float] = []
    last: float | None = None
    for i, spoken in enumerate(is_speech):
        if spoken:
            value = spoken_average(max(0, i - half), min(len(levels), i + half + 1))
        else:
            value = spoken_average(max(0, i - lookback), i)
        last = value if value is not None else last
        held.append(last)
    first = next(value for value in held if value is not None)
    held = [first if value is None else value for value in held]

    # Typical speech is 0: the median of the phrase level while speaking.
    spoken_levels = sorted(value for value, spoken in zip(held, is_speech) if spoken)
    reference = spoken_levels[len(spoken_levels) // 2]
    return [min(_FOLLOW_MAX_DB, max(_FOLLOW_MIN_DB, value - reference)) for value in held]


def _envelope_at(envelope: list[float], t_ms: float) -> float:
    if not envelope:
        return 0.0
    position = t_ms / _ENV_FRAME_MS - 0.5
    i = math.floor(position)
    if i < 0:
        return envelope[0]
    if i >= len(envelope) - 1:
        return envelope[-1]
    frac = position - i
    return envelope[i] + (envelope[i + 1] - envelope[i]) * frac


def _follow_narration(
    audio: AudioSegment,
    start_ms: int,
    envelope: list[float],
    base_db: float,
) -> AudioSegment:
    """Rescale ``audio`` every ``_FOLLOW_STEP_MS`` to track the narration under it.

    ``base_db`` is the average under-voice gain; the envelope moves it up or
    down. The result never rises above the catalog level.
    """

    frames_per_step = max(1, int(audio.frame_rate * _FOLLOW_STEP_MS / 1000))
    chunk = frames_per_step * audio.frame_width
    raw = audio.raw_data
    out: list[bytes] = []
    for k, offset in enumerate(range(0, len(raw), chunk)):
        t_ms = start_ms + (k + 0.5) * _FOLLOW_STEP_MS
        gain_db = min(0.0, base_db + _envelope_at(envelope, t_ms))
        out.append(audioop.mul(raw[offset : offset + chunk], audio.sample_width, 10 ** (gain_db / 20)))
    return audio._spawn(b"".join(out))


def _db_to_power(db: float) -> float:
    return 10 ** (db / 10)


def _power_to_db(power: float) -> float:
    return 10 * math.log10(power) if power > 0 else float("-inf")


def under_voice_gain_db(narration_lufs: float | None, effect_lufs: float) -> float:
    """Gain that puts an effect at ``effect_lufs`` ``SFX_UNDER_VOICE_LU`` below the narration.

    Never positive. Unmeasurable (silent) narration leaves effects at the catalog level.
    """

    if narration_lufs is None:
        return 0.0
    target = narration_lufs - SFX_UNDER_VOICE_LU
    return max(_SFX_MIN_GAIN_DB, min(0.0, target - effect_lufs))


def _overlay_clips(
    timeline: AudioSegment,
    clips: list[TimedClip],
    duration_ms: int,
    extra_gain_db: float = 0.0,
    envelope: list[float] | None = None,
) -> AudioSegment:
    for clip in clips:
        placed = _prepare_clip(clip, timeline, duration_ms, extra_gain_db, envelope)
        if placed is None:
            continue
        audio, start_ms = placed
        timeline = timeline.overlay(audio, position=start_ms)
    return _fit_length(timeline, duration_ms)


def _fit_length(timeline: AudioSegment, duration_ms: int) -> AudioSegment:
    if len(timeline) > duration_ms:
        return timeline[:duration_ms]
    if len(timeline) < duration_ms:
        pad = AudioSegment.silent(
            duration=duration_ms - len(timeline),
            frame_rate=timeline.frame_rate,
        )
        return timeline + pad
    return timeline


def export_loud_mp3(timeline: AudioSegment, output_path: Path) -> Path:
    """Export ``timeline`` as MP3 at ``OUTPUT_TARGET_LUFS``, peaks under ``_OUTPUT_TRUE_PEAK_DBTP``.

    Two-pass EBU R128 loudnorm. The first pass measures. The second applies one
    fixed gain when that fits under the peak ceiling; otherwise ffmpeg switches
    to its adaptive gain and limiter. Voice and effects move together, so their
    balance is kept. Silence is exported unchanged.
    """

    wav = _wav_bytes(timeline)
    target = f"I={OUTPUT_TARGET_LUFS}:TP={_OUTPUT_TRUE_PEAK_DBTP}:LRA={_OUTPUT_LRA}"
    measured = loudnorm_measure(wav, target)
    if measured is None:
        return export_mp3(timeline, output_path)
    apply = (
        f"loudnorm={target}"
        f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
        f":offset={measured['target_offset']}:linear=true"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
            "-i", "pipe:0",
            "-af", apply,
            "-ar", str(timeline.frame_rate),
            "-c:a", "libmp3lame", "-b:a", "128k",
            str(output_path),
        ],
        input=wav,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not output_path.is_file():
        detail = proc.stderr.decode("utf-8", errors="replace")[:300]
        raise AudioMixError(f"ffmpeg could not normalize the story mix: {detail}")
    return output_path


def loudnorm_measure(audio_bytes: bytes, target: str = "") -> dict | None:
    """ffmpeg loudnorm first-pass stats (``input_i``, ``input_tp``, ...).

    ``target`` is ``I=..:TP=..:LRA=..`` when the stats feed a second pass.
    None when the audio is silent or unreadable.
    """

    if not audio_bytes:
        return None
    spec = f"loudnorm={target}:print_format=json" if target else "loudnorm=print_format=json"
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", "pipe:0", "-af", spec, "-f", "null", "-"],
        input=audio_bytes,
        capture_output=True,
        check=False,
    )
    text = proc.stderr.decode("utf-8", errors="replace")
    start, end = text.rfind("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
        level = float(payload["input_i"])
    except (ValueError, KeyError, TypeError):
        return None
    if level != level or level < -70.0:
        return None
    return payload


def _wav_bytes(segment: AudioSegment) -> bytes:
    handle = io.BytesIO()
    segment.export(handle, format="wav")
    return handle.getvalue()


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
    extra_gain_db: float = 0.0,
    envelope: list[float] | None = None,
) -> tuple[AudioSegment, int] | None:
    start_ms = max(0, int(clip.start_ms)) + SFX_START_DELAY_MS
    end_ms = min(duration_ms, int(clip.end_ms) + SFX_START_DELAY_MS)
    window = end_ms - start_ms
    if window < _MIN_WINDOW_MS or start_ms >= duration_ms:
        return None
    audio = _match_format(load_clip(clip.audio_bytes), timeline)
    if clip.trim_start_ms > 0:
        audio = audio[clip.trim_start_ms :]
    if clip.gain_db:
        audio = audio.apply_gain(clip.gain_db)
    audio = audio[:window]
    if len(audio) <= 0:
        return None
    if envelope is not None:
        audio = _follow_narration(audio, start_ms, envelope, extra_gain_db)
    elif extra_gain_db:
        audio = audio.apply_gain(extra_gain_db)
    if len(audio) > _FADE_MS * 2:
        audio = audio.fade_in(_FADE_MS).fade_out(_FADE_MS)
    return audio, start_ms


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
