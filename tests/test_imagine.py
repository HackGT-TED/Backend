"""Grok Imagine cover prompt and client."""

import json

import httpx
import pytest

from app.config import Settings
from app.schemas.api import StoryCard
from app.services.imagine import HttpImagineClient, cover_prompt
from app.services.xai import XaiAuthError, XaiNotConfiguredError


def _card() -> StoryCard:
    return StoryCard(
        description="A short outdoor story where a dog barks.",
        hashtags=["animals", "funny"],
        scene="A spotted dog bounces beside a kid in a red raincoat.",
    )


def test_cover_prompt_stays_a_playful_picture_book():
    prompt = cover_prompt(_card(), duration_seconds=17.2)

    assert "Picture-book cover for ages 4 to 8" in prompt
    assert "A short outdoor story where a dog barks." in prompt
    assert "A spotted dog bounces beside a kid in a red raincoat." in prompt
    assert "animals, funny" in prompt
    assert "One small, punchy moment." in prompt
    assert "gouache" in prompt
    assert "No photoreal skin" in prompt
    assert "no watermark" in prompt


def test_generate_posts_imagine_and_returns_the_url():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.x.ai/v1/images/generations"
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["model"] == "grok-imagine-image-2.0"
        assert body["n"] == 1
        assert body["aspect_ratio"] == "1:1"
        assert body["quality"] == "medium"
        assert body["response_format"] == "url"
        assert "Picture-book cover" in body["prompt"]
        return httpx.Response(200, json={"data": [{"url": "https://im.example/cover.jpg"}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpImagineClient(
            Settings(xai_api_key="test-key", xai_image_model="grok-imagine-image-2.0"),
            http=http,
        )
        url = client.generate(cover_prompt(_card(), 30))

    assert url == "https://im.example/cover.jpg"


def test_missing_key_does_not_post():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpImagineClient(Settings(xai_api_key=""), http=http)
        with pytest.raises(XaiNotConfiguredError, match="XAI_API_KEY"):
            client.generate("a picture")
    assert calls["n"] == 0


def test_rejected_key_is_an_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "nope"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpImagineClient(Settings(xai_api_key="test-key"), http=http)
        with pytest.raises(XaiAuthError):
            client.generate("a picture")
