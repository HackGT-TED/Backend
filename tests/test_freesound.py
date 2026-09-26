"""FreeSound client against a mocked search and preview CDN."""

import json

import httpx
import pytest

from app.config import Settings
from app.services.freesound import (
    FreeSoundError,
    FreeSoundNotConfiguredError,
    FreeSoundRateLimitError,
    HttpFreeSoundClient,
)


def _settings(**overrides) -> Settings:
    data = {
        "freesound_api_key": "fs-key",
        "freesound_base_url": "https://freesound.org",
        "freesound_catalog_only": False,
    }
    data.update(overrides)
    return Settings(**data)


def _search_body() -> dict:
    return {
        "count": 1,
        "results": [
            {
                "id": 42,
                "name": "Old wooden door",
                "duration": 1.8,
                "previews": {
                    "preview-hq-mp3": "https://cdn.example/door.mp3",
                    "preview-lq-mp3": "https://cdn.example/door-lq.mp3",
                },
            }
        ],
    }


def test_missing_key_fails_before_any_request():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpFreeSoundClient(_settings(freesound_api_key="  "), http=http)
        with pytest.raises(FreeSoundNotConfiguredError, match="FREESOUND_API_KEY"):
            client.download_for_query("rain")
    assert calls["n"] == 0


def test_search_downloads_hq_preview_without_sending_the_token_to_the_cdn():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/apiv2/search/text/":
            assert request.headers["authorization"] == "Token fs-key"
            assert request.url.params["query"] == "wooden door creak"
            assert request.url.params["sort"] == "rating_desc"
            return httpx.Response(200, json=_search_body())
        assert str(request.url) == "https://cdn.example/door.mp3"
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"ID3preview-bytes")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpFreeSoundClient(_settings(), http=http)
        clip = client.download_for_query("  wooden   door creak  ")

    assert clip.sound_id == 42
    assert clip.name == "Old wooden door"
    assert clip.query == "wooden door creak"
    assert clip.preview_url == "https://cdn.example/door.mp3"
    assert clip.audio_bytes == b"ID3preview-bytes"
    assert clip.duration_seconds == 1.8


def test_rate_limit_is_retried_then_succeeds():
    calls = {"n": 0}
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/apiv2/search/text/":
            return httpx.Response(200, content=b"mp3")
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"detail": "slow down"})
        return httpx.Response(200, json=_search_body())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpFreeSoundClient(
            _settings(),
            http=http,
            sleeper=delays.append,
            max_retries=3,
        )
        clip = client.download_for_query("rain")

    assert clip.audio_bytes == b"mp3"
    assert calls["n"] == 2
    assert delays == [0.0]


def test_rate_limit_after_retries_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "slow down"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpFreeSoundClient(
            _settings(),
            http=http,
            sleeper=lambda _delay: None,
            max_retries=2,
        )
        with pytest.raises(FreeSoundRateLimitError):
            client.download_for_query("rain")


def test_no_results_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 0, "results": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpFreeSoundClient(_settings(), http=http, sleeper=lambda _delay: None)
        with pytest.raises(FreeSoundError, match="No FreeSound results"):
            client.download_for_query("rain")


def test_rejected_key_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpFreeSoundClient(_settings(), http=http, sleeper=lambda _delay: None)
        with pytest.raises(FreeSoundError, match="FREESOUND_API_KEY"):
            client.download_for_query("rain")


def test_catalog_only_is_the_default():
    assert Settings.model_fields["freesound_catalog_only"].default is True


def test_catalog_only_downloads_the_matched_preview_and_does_not_search(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "id": "rain",
                        "label": "Rain",
                        "keywords": ["rain", "drizzle"],
                        "status": "pending",
                        "freesound_id": 7,
                        "preview_url": "https://cdn.example/rain.mp3",
                        "duration": 4.0,
                    },
                    {
                        "id": "door-creak",
                        "label": "Door creak",
                        "keywords": ["door", "creak", "wooden"],
                        "status": "pending",
                        "freesound_id": 99,
                        "preview_url": "https://cdn.example/door.mp3",
                        "duration": 1.4,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert "/apiv2/search" not in str(request.url)
        assert "authorization" not in {key.lower() for key in request.headers}
        assert str(request.url) == "https://cdn.example/door.mp3"
        return httpx.Response(200, content=b"door-bytes")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpFreeSoundClient(
            _settings(freesound_api_key="", freesound_catalog_only=True, sfx_catalog_path=str(catalog)),
            http=http,
        )
        clip = client.download_for_query("wooden door creak")

    assert clip.sound_id == 99
    assert clip.name == "Door creak"
    assert clip.preview_url == "https://cdn.example/door.mp3"
    assert clip.audio_bytes == b"door-bytes"
    assert clip.duration_seconds == 1.4
    assert seen == ["https://cdn.example/door.mp3"]


def test_catalog_only_missing_preview_does_not_search(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "id": "door-creak",
                        "label": "Door creak",
                        "keywords": ["door", "creak"],
                        "status": "empty",
                        "freesound_id": None,
                        "preview_url": None,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"catalog-only mode made a request: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpFreeSoundClient(
            _settings(freesound_api_key="", freesound_catalog_only=True, sfx_catalog_path=str(catalog)),
            http=http,
        )
        with pytest.raises(FreeSoundError, match="build_sfx_catalog"):
            client.download_for_query("door creak")


def test_catalog_only_rejects_queries_outside_the_pack(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "id": "dog-bark",
                        "label": "Dog bark",
                        "keywords": ["dog", "bark"],
                        "status": "approved",
                        "freesound_id": 3,
                        "preview_url": "https://cdn.example/dog.mp3",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"catalog-only mode made a request: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HttpFreeSoundClient(
            _settings(freesound_catalog_only=True, sfx_catalog_path=str(catalog)),
            http=http,
        )
        with pytest.raises(FreeSoundError, match="does not search"):
            client.download_for_query("spaceship laser")
