"""Deepgram JSON normalization, including the checked-in sample fixture."""

import json
from pathlib import Path

import pytest

from app.schemas.deepgram import DeepgramTranscript

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "deepgram_sample.json"


def test_sample_fixture_matches_a_prerecorded_response():
    payload = json.loads(FIXTURE.read_text())
    transcript = DeepgramTranscript.model_validate(payload).normalized()

    assert transcript.text.startswith("Once upon a time the rain began to fall.")
    assert len(transcript.words) == 20
    assert transcript.words[0].display == "Once"
    assert transcript.words[5].word == "rain"
    assert transcript.words[5].start == 1.28
    assert transcript.words[8].display == "fall."
    assert [(segment.start, segment.end) for segment in transcript.segments] == [
        (0.0, 2.6),
        (3.4, 7.6),
    ]
    # metadata.duration is longer than the last word, and the timeline keeps it.
    assert transcript.duration_seconds == 8.5

    prompt = transcript.prompt_payload()
    assert prompt["words"][0] == {"word": "Once", "start": 0.0, "end": 0.32}
    assert prompt["duration_seconds"] == 8.5


def test_extra_deepgram_fields_are_ignored():
    payload = json.loads(FIXTURE.read_text())
    payload["results"]["channels"][0]["alternatives"][0]["words"][0]["speaker_confidence"] = 0.4
    payload["results"]["channels"][0]["alternatives"][0]["words"][0]["language"] = "en"
    transcript = DeepgramTranscript.model_validate(payload).normalized()
    assert transcript.words[0].display == "Once"


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
    assert [segment.text for segment in transcript.segments] == [
        "Once upon a time the rain began to fall.",
        "Grandma opened the creaky door and a little bird sang hello.",
    ]


def test_empty_transcript_is_rejected():
    with pytest.raises(ValueError, match="empty"):
        DeepgramTranscript.model_validate({"transcript": "   ", "words": []}).normalized()
