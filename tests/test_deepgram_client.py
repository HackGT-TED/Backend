"""Deepgram listen client against a mocked HTTP API and the sample fixture."""

import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.schemas.deepgram import DeepgramTranscript
from app.services.deepgram import (
    DeepgramAuthError,
    DeepgramNotConfiguredError,
    HttpDeepgramClient,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "deepgram_sample.json"


def _settings(**overrides) -> Settings:
    data = {
        "deepgram_api_key": "dg-key",
        "deepgram_base_url": "https://api.deepgram.com",
        "deepgram_model": "nova-3",
        "deepgram_language": "en",
    }
    data.update(overrides)
    return Settings(**data)


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def test_missing_key_fails_before_any_request():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpDeepgramClient(_settings(deepgram_api_key=""), http=http)
        with pytest.raises(DeepgramNotConfiguredError, match="DEEPGRAM_API_KEY"):
            client.transcribe_url("https://example.test/story.wav")
    assert calls["n"] == 0


def test_transcribe_url_requests_word_timestamps():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("https://api.deepgram.com/v1/listen?")
        assert request.headers["authorization"] == "Token dg-key"
        assert b"dg-key" not in request.content
        assert request.url.params["model"] == "nova-3"
        assert request.url.params["language"] == "en"
        assert request.url.params["utterances"] == "true"
        assert request.url.params["paragraphs"] == "true"
        assert request.url.params["smart_format"] == "true"
        assert json.loads(request.content) == {"url": "https://example.test/story.wav"}
        return httpx.Response(200, json=_fixture())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpDeepgramClient(_settings(), http=http)
        payload = client.transcribe_url(" https://example.test/story.wav ")

    transcript = DeepgramTranscript.model_validate(payload).normalized()
    assert transcript.words[5].word == "rain"
    assert transcript.words[5].start == 1.28
    assert transcript.words[5].end == 1.7
    assert transcript.segments[0].end == 2.6
    assert transcript.duration_seconds == 8.5


def test_transcribe_bytes_sends_the_audio_body():
    wav = b"RIFFfake"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["content-type"] == "audio/wav"
        assert request.content == wav
        return httpx.Response(200, json=_fixture())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpDeepgramClient(_settings(), http=http)
        payload = client.transcribe_bytes(wav, "audio/wav")

    assert payload["metadata"]["duration"] == 8.5


def test_rejected_key_is_an_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpDeepgramClient(_settings(), http=http)
        with pytest.raises(DeepgramAuthError, match="DEEPGRAM_API_KEY"):
            client.transcribe_url("https://example.test/story.wav")
