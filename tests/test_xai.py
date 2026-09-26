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
    LONG_PAUSE_MS,
    XaiAuthError,
    XaiError,
    XaiNotConfiguredError,
    planning_user_payload,
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
        item_schema = body["response_format"]["json_schema"]["schema"]["properties"]["cues"]["items"]
        assert "catalog_id" in item_schema["required"]
        assert "kind" in item_schema["properties"]
        assert "gain_db" in item_schema["properties"]
        assert "until_seconds" in item_schema["properties"]
        assert "end_at_scene_change" in item_schema["properties"]
        system = body["messages"][0]["content"]
        assert "ambient" in system
        assert "fill_pause" in system
        assert str(LONG_PAUSE_MS) in system
        user = json.loads(body["messages"][1]["content"])
        assert "The rain began" in user["transcript"]
        assert user["catalog"] == [{"id": "rain", "label": "Rain"}]
        assert user["long_pause_ms"] == LONG_PAUSE_MS
        assert user["pauses"]
        assert "preview_url" not in request.content.decode()
        assert "confidence" not in request.content.decode()
        assert "channels" not in user
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
        cues = client.plan_cues(
            _transcript(),
            [
                {
                    "id": "rain",
                    "label": "Rain",
                    "keywords": ["rain", "storm"],
                    "preview_url": "https://cdn.example/rain.mp3",
                }
            ],
        )

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


def test_planning_payload_is_timings_and_catalog_ids_only():
    transcript = _transcript()
    payload = planning_user_payload(
        transcript,
        [{"id": "door-creak", "label": "Door creak", "preview_url": "https://cdn.example/door.mp3"}],
    )
    assert set(payload) == {
        "transcript",
        "duration_seconds",
        "words",
        "segments",
        "pauses",
        "long_pause_ms",
        "catalog",
    }
    assert set(payload["words"][0]) == {"word", "start", "end"}
    assert set(payload["segments"][0]) == {"text", "start", "end"}
    assert payload["catalog"] == [{"id": "door-creak", "label": "Door creak"}]
    assert payload["pauses"][0]["gap_ms"] >= LONG_PAUSE_MS


def test_ambient_until_seconds_and_gain_are_kept():
    def handler(request: httpx.Request) -> httpx.Response:
        content = json.dumps(
            {
                "cues": [
                    {
                        "catalog_id": "rain",
                        "query": None,
                        "reason": "Rain until they come inside.",
                        "start": 0.75,
                        "end": None,
                        "kind": "ambient",
                        "gain_db": -8,
                        "end_at_scene_change": True,
                        "until_seconds": 9.96,
                    }
                ]
            }
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": content}}]},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpXaiClient(_settings(), http=http)
        cues = client.plan_cues(_transcript(), [{"id": "rain", "label": "Rain"}])

    assert len(cues) == 1
    assert cues[0].catalog_id == "rain"
    assert cues[0].kind == "ambient"
    assert cues[0].role == "ambient"
    assert cues[0].gain_db == -8
    assert cues[0].start_ms == 750
    assert cues[0].end_ms == 9960
    assert cues[0].reason == "Rain until they come inside."


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


def test_align_keeps_overlapping_cues_and_long_ambient_windows():
    aligned = align_cues(
        [
            SfxCue(
                catalog_id="rain",
                reason="Rain across the cottage scene.",
                kind="ambient",
                start=0.35,
                end=14.35,
                gain_db=-80,
            ),
            SfxCue(
                catalog_id="door-creak",
                reason="The door.",
                role="oneshot",
                start=5.5,
                end=6.4,
            ),
        ],
        duration_seconds=55.78,
    )
    assert [(cue.catalog_id, cue.start_ms, cue.end_ms, cue.kind) for cue in aligned] == [
        ("rain", 350, 14350, "ambient"),
        ("door-creak", 5500, 6400, "oneshot"),
    ]
    assert aligned[0].gain_db == -24


def test_ambient_without_an_end_holds_until_the_story_ends():
    aligned = align_cues(
        [
            SfxCue(
                catalog_id="rain",
                reason="Rain through the rest of the story.",
                kind="ambient",
                start=49.58,
                end_at_scene_change=True,
            )
        ],
        duration_seconds=55.78,
    )
    assert len(aligned) == 1
    assert aligned[0].start_ms == 49580
    assert aligned[0].end_ms == 55780
    assert aligned[0].kind == "ambient"


def test_explicit_scene_end_is_not_stretched_to_the_story():
    aligned = align_cues(
        [
            SfxCue(
                catalog_id="wind",
                reason="Wind until the door shuts.",
                kind="ambient",
                start=5.46,
                end=9.96,
                end_at_scene_change=True,
                until_seconds=40,
            )
        ],
        duration_seconds=55.78,
    )
    assert aligned[0].end_ms == 9960
