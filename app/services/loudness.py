"""Bring catalog previews to one loudness.

FreeSound previews are fetched as published. A short bark and a long rain bed
do not leave the CDN at the same level, so each slot stores ``gain_db``: the
offset that puts its loudest moment on ``TARGET_DBFS``. The mixer applies that
offset before it ducks the clip under the narration.
"""

from pydub import AudioSegment

from app.services.mixer import AudioMixError, load_clip

# Loudest 200 ms of each preview is moved to this level.
TARGET_DBFS = -20.0
_WINDOW_MS = 200
_STEP_MS = 50
# A nearly silent file is not turned into a blast. A clipped file can be cut harder.
_MAX_BOOST_DB = 12.0
_MAX_CUT_DB = 40.0


def level_match_gain_db(audio: AudioSegment, target_dbfs: float = TARGET_DBFS) -> float:
    """Gain in dB that puts the loudest part of ``audio`` on ``target_dbfs``."""

    level = _loudest_window_dbfs(audio)
    if level is None:
        return 0.0
    gain = target_dbfs - level
    gain = max(-_MAX_CUT_DB, min(_MAX_BOOST_DB, gain))
    return round(gain, 1)


def stored_gain_db(entry: dict | None) -> float | None:
    """Return a catalog entry's ``gain_db`` when it is a real number."""

    if not isinstance(entry, dict):
        return None
    value = entry.get("gain_db")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value:
        return None
    return float(value)


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

    try:
        audio = load_clip(audio_bytes)
    except AudioMixError:
        return None
    gain = level_match_gain_db(audio)
    entry["gain_db"] = gain
    return gain


def _loudest_window_dbfs(audio: AudioSegment) -> float | None:
    if len(audio) <= 0:
        return None
    if len(audio) <= _WINDOW_MS:
        return _finite_dbfs(audio)
    loudest = float("-inf")
    last = len(audio) - _WINDOW_MS
    for start in range(0, last + 1, _STEP_MS):
        level = audio[start : start + _WINDOW_MS].dBFS
        if level > loudest:
            loudest = level
    if last % _STEP_MS != 0:
        level = audio[last:].dBFS
        if level > loudest:
            loudest = level
    return _finite_dbfs_value(loudest)


def _finite_dbfs(audio: AudioSegment) -> float | None:
    return _finite_dbfs_value(audio.dBFS)


def _finite_dbfs_value(level: float) -> float | None:
    if level == float("-inf") or level != level:
        return None
    return level
