"""Timestamp alignment for the SFX-only timeline."""

from pydub import AudioSegment

from app.services.mixer import TimedClip, mix_sfx_mp3, place_clips
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
    # Margins avoid MP3-unrelated millisecond rounding at the cue boundary.
    assert _rms(timeline, 0, 1_900) == 0
    assert _rms(timeline, 2_100, 3_400) > 1_000
    assert _rms(timeline, 3_600, 6_900) == 0
    assert _rms(timeline, 7_100, 7_900) > 1_000
    assert _rms(timeline, 8_100, 10_000) == 0

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
    assert _rms(timeline, 0, 8_900) == 0
    assert _rms(timeline, 9_100, 9_900) > 1_000


def test_empty_cue_list_is_silence_of_the_story_duration():
    timeline = place_clips([], 4_000)
    assert len(timeline) == 4_000
    assert timeline.rms == 0
