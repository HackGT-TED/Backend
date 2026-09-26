"""Deepgram JSON normalization, including the checked-in sample fixture."""

import json
from pathlib import Path

import pytest

from app.schemas.deepgram import DeepgramTranscript

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "deepgram_sample.json"


def test_sample_fixture_matches_a_prerecorded_response():
    payload = json.loads(FIXTURE.read_text())
    transcript = DeepgramTranscript.model_validate(payload).normalized()

    assert transcript.text.startswith("The rain began to tap on the cottage roof.")
    assert len(transcript.words) == 88
    assert transcript.words[0].display == "The"
    assert transcript.words[1].word == "rain"
    assert transcript.words[1].start == 0.75
    assert transcript.words[1].end == 1.17
    assert transcript.segments[0].text == "The rain began to tap on the cottage roof."
    assert (transcript.segments[0].start, transcript.segments[0].end) == (0.35, 4.21)
    assert len(transcript.segments) == 12
    # metadata.duration is longer than the last word, and the timeline keeps it.
    assert transcript.duration_seconds == 55.78

    prompt = transcript.prompt_payload()
    assert prompt["words"][0] == {"word": "The", "start": 0.35, "end": 0.69}
    assert prompt["duration_seconds"] == 55.78

    pauses = transcript.pause_gaps(600)
    assert len(pauses) == 12
    assert pauses[0] == {
        "start": 4.21,
        "end": 5.46,
        "gap_ms": 1250,
        "after": "roof.",
        "before": "Grandma",
    }
    assert pauses[-1]["before"] == ""
    assert pauses[-1]["gap_ms"] == 2400
    assert transcript.pause_gaps(2000) == [pauses[-1]]


def test_extra_deepgram_fields_are_ignored():
    payload = json.loads(FIXTURE.read_text())
    payload["results"]["channels"][0]["alternatives"][0]["words"][0]["speaker_confidence"] = 0.4
    payload["results"]["channels"][0]["alternatives"][0]["words"][0]["language"] = "en"
    transcript = DeepgramTranscript.model_validate(payload).normalized()
    assert transcript.words[0].display == "The"


def test_simplified_transcript_with_word_timestamps():
    transcript = DeepgramTranscript.model_validate(
        {
            "transcript": "The door creaked.",
            "duration": 3,
            "words": [
                {"word": "the", "start": 0.0, "end": 0.2, "punctuated_word": "The"},
                {"word": "door", "start": 0.2, "end": 0.6},
                {"word": "creaked", "start": 0.6, "end": 1.2, "punctuated_word": "creaked."},
            ],
            "segments": [{"text": "The door creaked.", "start": 0.0, "end": 1.2}],
        }
    ).normalized()

    assert transcript.text == "The door creaked."
    assert transcript.duration_seconds == 3
    assert transcript.segments[0].end == 1.2


def test_paragraph_sentences_are_used_when_utterances_are_absent():
    payload = json.loads(FIXTURE.read_text())
    del payload["results"]["utterances"]
    transcript = DeepgramTranscript.model_validate(payload).normalized()
    assert [segment.text for segment in transcript.segments[:2]] == [
        "The rain began to tap on the cottage roof.",
        "Grandma opened the creaky door, and the wind slipped inside.",
    ]


def test_empty_transcript_is_rejected():
    with pytest.raises(ValueError, match="empty"):
        DeepgramTranscript.model_validate({"transcript": "   ", "words": []}).normalized()
