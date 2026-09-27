"""Timestamp alignment for the SFX-only timeline."""

from pydub import AudioSegment

import pytest

from app.services.mixer import (
    MOMENT_GAP_MS,
    OUTPUT_TARGET_LUFS,
    SFX_START_DELAY_MS,
    AudioJoinError,
    SFX_UNDER_VOICE_LU,
    TimedClip,
    join_audio,
    load_clip,
    loudnorm_measure,
    mix_on_story_bytes,
    mix_sfx_mp3,
    narration_envelope_db,
    overlay_on_story,
    place_clips,
    under_voice_gain_db,
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
        gain_db=-18,
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


def test_effects_sit_a_fixed_amount_under_the_narration():
    assert under_voice_gain_db(-30.0, -20.0) == -30.0 - SFX_UNDER_VOICE_LU + 20.0
    assert under_voice_gain_db(-40.0, -20.0) < under_voice_gain_db(-30.0, -20.0)
    assert under_voice_gain_db(-5.0, -20.0) == 0
    assert under_voice_gain_db(None, -20.0) == 0
    assert under_voice_gain_db(-90.0, -20.0) == -40.0


def test_a_mixed_effect_lands_well_under_a_quiet_voice():
    """A catalog-level effect on a quiet recording ends up about SFX_UNDER_VOICE_LU below it."""

    from app.services.loudness import TARGET_LUFS, integrated_lufs, level_match_gain_db

    story_bytes = sine_wav_bytes(6_000, frequency=300, amplitude=0.03)
    effect_bytes = sine_wav_bytes(3_000, frequency=1000, amplitude=0.5)
    match = level_match_gain_db(load_clip(effect_bytes))
    clip = TimedClip(start_ms=1_000, end_ms=4_000, audio_bytes=effect_bytes, gain_db=match)

    silence = AudioSegment.silent(duration=6_000, frame_rate=44100)
    only_effect = overlay_on_story(_wav(silence), [clip])
    assert abs(integrated_lufs(_wav(only_effect)) - TARGET_LUFS) < 1.5

    voice_lufs = integrated_lufs(story_bytes)
    mixed = overlay_on_story(story_bytes, [clip])
    start = 1_000 + SFX_START_DELAY_MS + 100
    effect_part = mixed[start : start + 2_500].overlay(
        load_clip(story_bytes)[start : start + 2_500].invert_phase()
    )
    effect_lufs = integrated_lufs(_wav(effect_part))
    assert abs((voice_lufs - effect_lufs) - SFX_UNDER_VOICE_LU) < 2.0


def test_trim_start_skips_the_lead_in_so_the_sound_plays_at_the_cue():
    lead_in = AudioSegment.silent(duration=2_500, frame_rate=44100)
    growl = load_clip(sine_wav_bytes(2_000, frequency=200, amplitude=0.5))
    clip_bytes = _wav(lead_in + growl)
    start = 1_000 + SFX_START_DELAY_MS

    untrimmed = place_clips([TimedClip(1_000, 3_000, clip_bytes)], 4_000)
    trimmed = place_clips([TimedClip(1_000, 3_000, clip_bytes, trim_start_ms=2_500)], 4_000)

    assert untrimmed[start + 50 : start + 1_500].rms == 0
    assert trimmed[start + 50 : start + 1_500].rms > 1_000


def test_a_quiet_recording_comes_out_at_the_output_loudness():
    story_bytes = sine_wav_bytes(8_000, frequency=300, amplitude=0.02)
    assert float(loudnorm_measure(story_bytes)["input_i"]) < OUTPUT_TARGET_LUFS - 15

    out = loudnorm_measure(mix_on_story_bytes(story_bytes, []))

    assert abs(float(out["input_i"]) - OUTPUT_TARGET_LUFS) < 1.0
    assert float(out["input_tp"]) <= -1.0


def test_a_peaky_quiet_recording_gets_louder_without_clipping():
    """One near-full-scale click blocks a plain gain boost; the limiter path still gets it loud."""

    quiet = load_clip(sine_wav_bytes(8_000, frequency=300, amplitude=0.05))
    click = load_clip(sine_wav_bytes(5, frequency=300, amplitude=0.95))
    story = quiet.overlay(click, position=4_000)
    before = loudnorm_measure(_wav(story))
    assert float(before["input_tp"]) > -1.5

    out = loudnorm_measure(mix_on_story_bytes(_wav(story), []))

    assert float(out["input_i"]) > float(before["input_i"]) + 8
    assert float(out["input_tp"]) <= -1.0


def test_a_silent_recording_still_exports():
    silence = AudioSegment.silent(duration=2_000, frame_rate=44100)
    mixed = load_clip(mix_on_story_bytes(_wav(silence), []))
    assert abs(len(mixed) - 2_000) < 100


def _effect_only(mixed: AudioSegment, story: AudioSegment, start: int, end: int) -> AudioSegment:
    return mixed[start:end].overlay(story[start:end].invert_phase())


def test_effect_drops_when_the_narrator_whispers_and_rises_when_they_speak_up():
    loud = load_clip(sine_wav_bytes(4_000, frequency=300, amplitude=0.3))
    whisper = load_clip(sine_wav_bytes(4_000, frequency=300, amplitude=0.03))
    story = loud + whisper
    effect_bytes = sine_wav_bytes(8_000, frequency=1000, amplitude=0.5)
    from app.services.loudness import level_match_gain_db

    match = level_match_gain_db(load_clip(effect_bytes))
    clip = TimedClip(start_ms=0, end_ms=8_000, audio_bytes=effect_bytes, gain_db=match)

    mixed = overlay_on_story(_wav(story), [clip])
    during_loud = _effect_only(mixed, story, 1_000, 3_000)
    during_whisper = _effect_only(mixed, story, 5_500, 7_500)

    assert during_whisper.dBFS < during_loud.dBFS - 12
    assert during_loud.dBFS < loud.dBFS - 6
    assert during_whisper.dBFS < whisper.dBFS - 6


def test_a_pause_holds_the_effect_at_the_last_spoken_level():
    talk = load_clip(sine_wav_bytes(3_000, frequency=300, amplitude=0.1))
    pause = AudioSegment.silent(duration=3_000, frame_rate=44100)
    story = talk + pause + talk
    clip = TimedClip(start_ms=0, end_ms=9_000, audio_bytes=sine_wav_bytes(9_000, frequency=1000, amplitude=0.5))

    mixed = overlay_on_story(_wav(story), [clip])
    during_talk = _effect_only(mixed, story, 1_000, 2_500)
    during_pause = _effect_only(mixed, story, 3_700, 5_300)

    assert abs(during_pause.dBFS - during_talk.dBFS) < 2


def test_steady_narration_gives_a_flat_envelope():
    story = load_clip(sine_wav_bytes(5_000, frequency=300, amplitude=0.1))
    envelope = narration_envelope_db(story)
    assert max(abs(value) for value in envelope) < 0.5


def _wav(segment: AudioSegment) -> bytes:
    import io

    handle = io.BytesIO()
    segment.export(handle, format="wav")
    return handle.getvalue()


def test_catalog_gain_puts_a_quiet_clip_at_the_same_level_as_a_loud_one():
    from app.services.loudness import level_match_gain_db

    loud = sine_wav_bytes(1_000, frequency=440, amplitude=0.8)
    quiet = sine_wav_bytes(1_000, frequency=880, amplitude=0.05)
    loud_gain = level_match_gain_db(load_clip(loud))
    quiet_gain = level_match_gain_db(load_clip(quiet))
    timeline = place_clips(
        [
            TimedClip(start_ms=0, end_ms=800, audio_bytes=loud, query="loud", gain_db=loud_gain),
            TimedClip(start_ms=2_000, end_ms=2_800, audio_bytes=quiet, query="quiet", gain_db=quiet_gain),
        ],
        4_000,
    )
    loud_rms = _rms(timeline, 200, 600)
    quiet_rms = _rms(timeline, 2_200, 2_600)
    assert loud_rms > 0 and quiet_rms > 0
    ratio = loud_rms / quiet_rms
    assert 0.8 <= ratio <= 1.25


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
