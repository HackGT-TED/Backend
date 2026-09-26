"""Timestamp alignment for the SFX-only timeline."""

from pydub import AudioSegment

import pytest

from app.services.mixer import (
    MOMENT_GAP_MS,
    SFX_START_DELAY_MS,
    AudioJoinError,
    TimedClip,
    effect_gain_db,
    join_audio,
    load_clip,
    mix_sfx_mp3,
    overlay_on_story,
    place_clips,
)
from tests.wavutil import sine_wav_bytes


def _rms(timeline: AudioSegment, start_ms: int, end_ms: int) -> float:
    return timeline[start_ms:end_ms].rms


def test_timestamp_alignment_places_effects_on_the_story_clock(tmp_path):
    """Effects sound only inside their cue windows, and the story length stays put.

    Each source clip is longer than its cue so a missed trim would bleed into
    the following silence. This is the integration-style check that downloaded
    audio is aligned to Deepgram timestamps before it is exported.
    """

    duration_ms = 10_000
    rain = TimedClip(
        start_ms=2_000,
        end_ms=3_500,
        audio_bytes=sine_wav_bytes(5_000, frequency=440),
        query="gentle rain ambience",
    )
    door = TimedClip(
        start_ms=7_000,
        end_ms=8_000,
        audio_bytes=sine_wav_bytes(4_000, frequency=880),
        query="wooden door creak",
    )

    timeline = place_clips([rain, door], duration_ms)

    assert len(timeline) == duration_ms
    # Playback starts SFX_START_DELAY_MS after the cue, and ends that much later.
    assert _rms(timeline, 0, 2_000 + SFX_START_DELAY_MS - 40) == 0
    assert _rms(timeline, 2_000 + SFX_START_DELAY_MS + 80, 3_400) > 1_000
    assert _rms(timeline, 3_500 + SFX_START_DELAY_MS + 80, 6_900) == 0
    assert _rms(timeline, 7_000, 7_000 + SFX_START_DELAY_MS - 40) == 0
    assert _rms(timeline, 7_000 + SFX_START_DELAY_MS + 80, 7_900) > 1_000
    assert _rms(timeline, 8_000 + SFX_START_DELAY_MS + 80, 10_000) == 0

    output = tmp_path / "sfx.mp3"
    mix_sfx_mp3([rain, door], duration_ms, output)
    exported = AudioSegment.from_mp3(output)
    # MP3 framing adds a small amount of encoder delay around the exact timeline.
    assert output.stat().st_size > 500
    assert abs(len(exported) - duration_ms) < 500


def test_cues_that_run_past_the_story_are_clamped():
    duration_ms = 10_000
    timeline = place_clips(
        [
            TimedClip(
                start_ms=9_000,
                end_ms=12_000,
                audio_bytes=sine_wav_bytes(5_000, frequency=440),
                query="bird song",
            )
        ],
        duration_ms,
    )

    assert len(timeline) == duration_ms
    assert _rms(timeline, 0, 9_000 + SFX_START_DELAY_MS - 40) == 0
    assert _rms(timeline, 9_000 + SFX_START_DELAY_MS + 80, 9_950) > 1_000


def test_mp3_bytes_decode_without_ffprobe(tmp_path):
    output = tmp_path / "sfx.mp3"
    mix_sfx_mp3(
        [
            TimedClip(
                start_ms=0,
                end_ms=800,
                audio_bytes=sine_wav_bytes(800, frequency=440),
                query="rain",
            )
        ],
        1_000,
        output,
    )
    clip = load_clip(output.read_bytes())
    assert len(clip) > 500


def test_effects_start_late_and_stay_under_the_narration():
    """The bark waits past the word start, and it does not jump over the voice."""

    story_bytes = sine_wav_bytes(4_000, frequency=220, amplitude=0.2)
    story = load_clip(story_bytes)
    effect = TimedClip(
        start_ms=1_000,
        end_ms=2_000,
        audio_bytes=sine_wav_bytes(3_000, frequency=1400, amplitude=0.95),
        query="dog-bark",
    )

    mixed = overlay_on_story(story_bytes, [effect])
    play_at = 1_000 + SFX_START_DELAY_MS

    assert len(mixed) == 4_000
    lead = mixed[1_000 : play_at - 20]
    voice_lead = story[1_000 : play_at - 20]
    assert abs(lead.dBFS - voice_lead.dBFS) < 0.5

    played = mixed[play_at + 40 : 1_900]
    voice = story[play_at + 40 : 1_900]
    assert played.rms > voice.rms
    assert played.dBFS < voice.dBFS + 3

    after = mixed[2_000 + SFX_START_DELAY_MS + 80 : 3_800]
    assert abs(after.dBFS - story[2_000 + SFX_START_DELAY_MS + 80 : 3_800].dBFS) < 0.5


def test_quieter_narration_ducks_the_effect_further():
    effect = load_clip(sine_wav_bytes(1_000, frequency=1400, amplitude=0.95))
    loud = load_clip(sine_wav_bytes(1_000, frequency=220, amplitude=0.5))
    quiet = load_clip(sine_wav_bytes(1_000, frequency=220, amplitude=0.05))
    silent = AudioSegment.silent(duration=1_000, frame_rate=44100)

    gain_loud = effect_gain_db(loud, effect)
    gain_quiet = effect_gain_db(quiet, effect)
    ducked_loud = effect.apply_gain(gain_loud)
    ducked_quiet = effect.apply_gain(gain_quiet)

    assert gain_quiet < gain_loud <= 0
    assert ducked_loud.dBFS <= loud.dBFS - 10
    assert ducked_quiet.dBFS <= quiet.dBFS - 10
    assert ducked_quiet.dBFS < ducked_loud.dBFS - 6
    assert effect.apply_gain(effect_gain_db(silent, effect)).dBFS < -24


def test_an_already_quiet_effect_is_not_boosted():
    voice = load_clip(sine_wav_bytes(800, frequency=220, amplitude=0.5))
    effect = load_clip(sine_wav_bytes(800, frequency=1400, amplitude=0.01))
    assert effect_gain_db(voice, effect) == 0


def test_empty_cue_list_is_silence_of_the_story_duration():
    timeline = place_clips([], 4_000)
    assert len(timeline) == 4_000
    assert timeline.rms == 0


def test_join_audio_keeps_moment_order_with_a_pause_between():
    """Two moments become one recording: first, a silent gap, then the second."""

    first = sine_wav_bytes(1_000, frequency=440)
    second = sine_wav_bytes(1_500, frequency=880)
    joined = load_clip(join_audio([first, second]))

    # MP3 encoding pads slightly, so allow a little slack on the length.
    expected = 1_000 + MOMENT_GAP_MS + 1_500
    assert expected - 20 <= len(joined) <= expected + 120
    assert _rms(joined, 100, 900) > 1_000
    assert _rms(joined, 1_050, 1_000 + MOMENT_GAP_MS - 50) < 50
    assert _rms(joined, 1_000 + MOMENT_GAP_MS + 100, expected - 100) > 1_000


def test_join_audio_names_the_moment_that_cannot_be_decoded():
    with pytest.raises(AudioJoinError) as caught:
        join_audio([sine_wav_bytes(500), b"not audio at all"])
    assert caught.value.index == 2
    assert "Audio file 2" in str(caught.value)
