"""xAI client against a mocked Chat Completions endpoint."""

import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.schemas.deepgram import DeepgramTranscript
from app.schemas.sfx import align_cues, SfxCue
from app.services.xai import (
    HttpXaiClient,
    XaiAuthError,
    XaiError,
    XaiNotConfiguredError,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "deepgram_sample.json"


def _transcript():
    payload = json.loads(FIXTURE.read_text())
    return DeepgramTranscript.model_validate(payload).normalized()


def _settings(**overrides) -> Settings:
    data = {
        "xai_api_key": "test-key",
        "xai_base_url": "https://api.x.ai/v1",
        "xai_model": "grok-4.7",
    }
    data.update(overrides)
    return Settings(**data)


def test_missing_key_fails_before_any_request():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpXaiClient(_settings(xai_api_key=""), http=http)
        with pytest.raises(XaiNotConfiguredError, match="XAI_API_KEY"):
            client.plan_cues(_transcript())
    assert calls["n"] == 0


def test_plan_cues_posts_json_schema_and_aligns_to_the_story():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.x.ai/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        assert b"test-key" not in request.content
        body = json.loads(request.content)
        assert body["model"] == "grok-4.7"
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["name"] == "sfx_plan"
        assert "catalog_id" in body["response_format"]["json_schema"]["schema"]["properties"]["cues"]["items"]["required"]
        user = json.loads(body["messages"][1]["content"])
        assert "The rain began" in user["transcript"]
        assert user["catalog"] == [{"id": "rain", "label": "Rain"}]
        content = json.dumps(
            {
                "cues": [
                    {
                        "catalog_id": "rain",
                        "description": "Rain while the story mentions rain.",
                        "start": 1.28,
                        "end": 2.6,
                    },
                    {
                        "catalog_id": "past-the-ending",
                        "description": "This window is outside the story.",
                        "start": 80,
                        "end": 81,
                    },
                ]
            }
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": f"```json\n{content}\n```"}}]},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpXaiClient(_settings(), http=http)
        cues = client.plan_cues(_transcript(), [{"id": "rain", "label": "Rain"}])

    assert len(cues) == 1
    assert cues[0].catalog_id == "rain"
    assert cues[0].query == "rain"
    assert cues[0].start_ms == 1280
    assert cues[0].end_ms == 2600
    assert cues[0].start == 1.28
    assert cues[0].end == 2.6


def test_json_wrapped_in_prose_still_parses():
    cue = {
        "query": "gentle rain ambience",
        "description": "Rain while the story mentions rain.",
        "start": 1.28,
        "end": 2.6,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        content = "Here is the plan:\n" + json.dumps({"cues": [cue]}) + "\nDone."
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": content}}]},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpXaiClient(_settings(), http=http)
        cues = client.plan_cues(_transcript())

    assert len(cues) == 1
    assert cues[0].query == "gentle rain ambience"
    assert cues[0].start_ms == 1280


def test_rejected_key_is_an_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpXaiClient(_settings(), http=http)
        with pytest.raises(XaiAuthError):
            client.plan_cues(_transcript())


def test_empty_completion_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpXaiClient(_settings(), http=http)
        with pytest.raises(XaiError, match="empty"):
            client.plan_cues(_transcript())


def test_align_cues_clamps_the_start_and_drops_tiny_windows():
    aligned = align_cues(
        [
            SfxCue(query="door", description="door", start=-1, end=1.2),
            SfxCue(query="blip", description="too short", start=1.0, end=1.01),
        ],
        duration_seconds=8.5,
    )
    assert len(aligned) == 1
    assert aligned[0].start_ms == 0
    assert aligned[0].end_ms == 1200
