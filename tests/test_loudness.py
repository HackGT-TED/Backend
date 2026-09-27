"""Catalog previews are shifted onto one loudness."""

from pydub import AudioSegment

from app.services.loudness import (
    TARGET_DBFS,
    gain_for_entry,
    level_match_gain_db,
    record_clip_gain,
)
from app.services.mixer import load_clip
from tests.wavutil import sine_wav_bytes


def test_loud_and_quiet_clips_meet_the_same_level():
    loud = load_clip(sine_wav_bytes(800, amplitude=0.8))
    quiet = load_clip(sine_wav_bytes(800, amplitude=0.05))
    loud_gain = level_match_gain_db(loud)
    quiet_gain = level_match_gain_db(quiet)

    assert quiet_gain > loud_gain
    assert abs(loud.apply_gain(loud_gain).dBFS - TARGET_DBFS) < 1.5
    assert abs(quiet.apply_gain(quiet_gain).dBFS - TARGET_DBFS) < 1.5


def test_silence_is_left_alone_and_a_whisper_is_not_boosted_without_limit():
    silent = AudioSegment.silent(duration=500, frame_rate=44100)
    whisper = load_clip(sine_wav_bytes(400, amplitude=0.001))

    assert level_match_gain_db(silent) == 0.0
    assert level_match_gain_db(whisper) == 12.0


def test_stored_catalog_gain_wins_over_a_fresh_measurement():
    audio = load_clip(sine_wav_bytes(500, amplitude=0.2))
    entry = {"id": "dog-bark", "gain_db": -4.5}

    assert gain_for_entry(entry, audio) == -4.5
    assert gain_for_entry({"id": "rain"}, audio) == level_match_gain_db(audio)


def test_record_clip_gain_writes_the_offset_onto_the_entry():
    entry: dict = {"id": "door-creak"}
    gain = record_clip_gain(entry, sine_wav_bytes(600, amplitude=0.4))

    assert gain == entry["gain_db"]
    assert isinstance(gain, float)
    leveled = load_clip(sine_wav_bytes(600, amplitude=0.4)).apply_gain(gain)
    assert abs(leveled.dBFS - TARGET_DBFS) < 1.5
