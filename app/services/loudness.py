"""Bring catalog previews to one loudness.

FreeSound previews are fetched as published. A telephone can sit near full
scale for the whole ring, while a growl is mostly quiet, so matching the
single loudest instant leaves them far apart. Each slot stores ``gain_db``:
the offset that puts the clip's integrated loudness on ``TARGET_LUFS``.

``sfx_level_db`` on the catalog document is the one extra factor applied to
every clip when it is mixed onto a story. The leveled preview files do not
include that factor, so the folder is the matched reference and the number
in the JSON is the knob.
"""

import io
import json
import subprocess

from pydub import AudioSegment

from app.services.mixer import AudioMixError, ffmpeg_exe, load_clip

# Integrated loudness (LUFS) shared by every catalog preview.
TARGET_LUFS = -20.0
# Extra gain on every effect during a mix. 0 plays the matched preview level.
DEFAULT_SFX_LEVEL_DB = 0.0
_MAX_BOOST_DB = 24.0
_MAX_CUT_DB = 40.0
# Leave a little room under full scale after the match gain.
_PEAK_CEILING_DBFS = -1.0


def level_match_gain_db(audio: AudioSegment, target_lufs: float = TARGET_LUFS) -> float:
    """Gain in dB that puts ``audio`` on ``target_lufs``."""

    wav = _wav_bytes(audio)
    level = integrated_lufs(wav)
    if level is None:
        return 0.0
    return _clamp_gain(target_lufs - level, audio)


def stored_gain_db(entry: dict | None) -> float | None:
    """Return a catalog entry's ``gain_db`` when it is a real number."""

    if not isinstance(entry, dict):
        return None
    return _finite_number(entry.get("gain_db"))


def sfx_level_db(catalog: dict | None) -> float:
    """The single mix offset shared by every effect. Missing means 0."""

    if not isinstance(catalog, dict):
        return DEFAULT_SFX_LEVEL_DB
    value = _finite_number(catalog.get("sfx_level_db"))
    if value is None:
        return DEFAULT_SFX_LEVEL_DB
    return value


def gain_for_entry(entry: dict | None, audio: AudioSegment | None = None) -> float:
    """Use the catalog offset. Measure ``audio`` when the slot has none yet."""

    stored = stored_gain_db(entry)
    if stored is not None:
        return stored
    if audio is None or len(audio) <= 0:
        return 0.0
    return level_match_gain_db(audio)


def record_clip_gain(entry: dict, audio_bytes: bytes) -> float | None:
    """Measure ``audio_bytes`` and store ``gain_db`` on ``entry``.

    Return None when the bytes cannot be decoded. The entry is left unchanged
    in that case.
    """

    level = integrated_lufs(audio_bytes)
    try:
        audio = load_clip(audio_bytes)
    except AudioMixError:
        return None
    if level is None:
        gain = level_match_gain_db(audio)
    else:
        gain = _clamp_gain(TARGET_LUFS - level, audio)
    entry["gain_db"] = gain
    return gain


def leveled_preview_bytes(audio_bytes: bytes, gain_db: float) -> bytes:
    """MP3 of ``audio_bytes`` after ``gain_db``. This is the file to listen to."""

    audio = load_clip(audio_bytes)
    if gain_db:
        audio = audio.apply_gain(gain_db)
    handle = io.BytesIO()
    audio.export(handle, format="mp3", bitrate="128k")
    return handle.getvalue()


def integrated_lufs(audio_bytes: bytes) -> float | None:
    """EBU R128 integrated loudness, or None when the file is silent."""

    if not audio_bytes:
        return None
    proc = subprocess.run(
        [
            ffmpeg_exe(),
            "-hide_banner",
            "-i",
            "pipe:0",
            "-af",
            "loudnorm=print_format=json",
            "-f",
            "null",
            "-",
        ],
        input=audio_bytes,
        capture_output=True,
        check=False,
    )
    text = proc.stderr.decode("utf-8", errors="replace")
    start = text.rfind("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
        level = float(payload["input_i"])
    except (ValueError, KeyError, TypeError):
        return None
    if level != level or level < -70.0:
        return None
    return level


def _clamp_gain(gain: float, audio: AudioSegment) -> float:
    peak = audio.max_dBFS
    if peak != float("-inf") and peak == peak:
        gain = min(gain, _PEAK_CEILING_DBFS - peak)
    gain = max(-_MAX_CUT_DB, min(_MAX_BOOST_DB, gain))
    return round(gain, 1)


def _wav_bytes(audio: AudioSegment) -> bytes:
    handle = io.BytesIO()
    audio.export(handle, format="wav")
    return handle.getvalue()


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value:
        return None
    return float(value)
