"""Story API with xAI and FreeSound replaced by in-process fakes."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.schemas.api import StoryBlurb, StoryCard
from app.schemas.sfx import SfxCue
from app.services.sfx_catalog import CATALOG_PATH, load_catalog
from app.services.xai import XaiNotConfiguredError
from app.services.freesound import (
    DownloadedClip,
    FreeSoundError,
    FreeSoundRateLimitError,
)
from tests.fakes import MemoryRecordingStore
from tests.wavutil import sine_wav_bytes

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "deepgram_sample.json"


class FakeXai:
    def __init__(self, cues: list[SfxCue]) -> None:
        self.cues = cues
        self.calls = 0

    def plan_cues(self, transcript, catalog=None):
        self.calls += 1
        assert "rain" in transcript.text
        assert catalog is None or any(item.get("id") == "rain" for item in catalog)
        return self.cues


class FakeFreeSound:
    def __init__(self, fail_queries: dict[str, Exception] | None = None) -> None:
        self.fail_queries = fail_queries or {}
        self.queries: list[str] = []

    def download_for_query(self, query: str) -> DownloadedClip:
        self.queries.append(query)
        if query in self.fail_queries:
            raise self.fail_queries[query]
        return DownloadedClip(
            sound_id=7,
            name=query,
            query=query,
            preview_url="https://cdn.example/fake.wav",
            audio_bytes=sine_wav_bytes(3_000, frequency=440),
            duration_seconds=3.0,
        )


def _settings(**overrides) -> Settings:
    data = {
        "supabase_url": "https://example.supabase.co",
        "supabase_service_role_key": "service-role-test",
        "xai_api_key": "test",
        "freesound_api_key": "test",
    }
    data.update(overrides)
    return Settings(**data)


def _client(xai, freesound, deepgram=None, settings=None, imagine=None) -> tuple[TestClient, MemoryRecordingStore]:
    app, store = _app(xai, freesound, deepgram, settings=settings, imagine=imagine)
    return TestClient(app), store


def _payload(**extra) -> dict:
    body = {
        "story_id": "story-1",
        "title": "Rain story",
        "narrator": "Grandma",
        "source_audio_url": "https://example.test/original.wav",
        "deepgram": json.loads(FIXTURE.read_text()),
    }
    body.update(extra)
    return body


class FakeDeepgram:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.urls: list[str] = []
        self.audio: list[tuple[int, str]] = []

    def transcribe_url(self, url: str) -> dict:
        self.urls.append(url)
        return self.payload

    def transcribe_bytes(self, audio: bytes, content_type: str) -> dict:
        self.audio.append((len(audio), content_type))
        return self.payload


def _file_catalog() -> dict:
    return load_catalog(CATALOG_PATH)


def _app(xai, freesound, deepgram=None, catalog_loader=None, settings=None, imagine=None):
    store = MemoryRecordingStore()
    app = create_app(
        settings=settings or _settings(),
        xai_client=xai,
        freesound_client=freesound,
        store=store,
        deepgram_client=deepgram,
        catalog_loader=catalog_loader or _file_catalog,
        imagine_client=imagine,
    )
    return app, store


def test_health():
    client, _store = _client(FakeXai([]), FakeFreeSound())
    with client:
        assert client.get("/health").json() == {"status": "ok"}


def test_process_stores_recording_and_serves_aligned_sfx():
    xai = FakeXai(
        [
            SfxCue(
                query="rain",
                description="Rain under the first sentence.",
                start=1.28,
                end=2.6,
            ),
            SfxCue(
                query="door-creak",
                description="The creaky door.",
                start=6.82,
                end=8.0,
            ),
        ]
    )
    freesound = FakeFreeSound()
    client, store = _client(xai, freesound)
    with client:
        created = client.post("/stories/process", json=_payload())
        assert created.status_code == 201
        body = created.json()
        assert body["status"] == "ready"
        assert body["story_id"] == "story-1"
        assert body["narrator"] == "Grandma"
        assert body["duration_seconds"] == 55.78
        assert body["transcript_text"].startswith("The rain began")
        assert [cue["start_ms"] for cue in body["cues"]] == [1280, 6820]
        assert [cue["end_ms"] for cue in body["cues"]] == [2600, 8000]
        assert body["warnings"] == []
        assert body["sfx_url"] == (
            "https://example.supabase.co/storage/v1/object/public/story-sfx/"
            f"{body['id']}/sfx.mp3"
        )

        listed = client.get("/stories")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [body["id"]]
        assert listed.json()[0]["sfx_url"] == body["sfx_url"]

        filtered = client.get("/stories", params={"story_id": "other"})
        assert filtered.json() == []

        detail = client.get(f"/stories/{body['id']}")
        assert detail.status_code == 200
        assert detail.json()["transcript_json"]["metadata"]["duration"] == 55.78
        assert detail.json()["source_audio_url"] == "https://example.test/original.wav"

        sfx = client.get(f"/stories/{body['id']}/sfx", follow_redirects=False)
        assert sfx.status_code == 307
        assert sfx.headers["location"] == body["sfx_url"]
        stored = store.files[f"{body['id']}/sfx.mp3"]
        assert len(stored) > 500
        assert stored[:3] == b"ID3" or stored[0] == 0xFF

        missing = client.get("/stories/does-not-exist")
        assert missing.status_code == 404
        assert client.get("/stories/does-not-exist/sfx").status_code == 404

    assert xai.calls == 1
    assert freesound.queries == ["rain", "door-creak"]
    assert f"{body['id']}/sfx.mp3" in store.files


def test_raw_deepgram_body_is_accepted():
    xai = FakeXai(
        [SfxCue(query="bird-chirp", description="The bird.", start=6.3, end=7.6)]
    )
    client, _store = _client(xai, FakeFreeSound())
    with client:
        created = client.post("/stories/process", json=json.loads(FIXTURE.read_text()))
        assert created.status_code == 201
        assert created.json()["story_id"] is None
        assert created.json()["cues"][0]["query"] == "bird-chirp"


def test_invalid_transcript_is_422_and_stores_nothing():
    client, store = _client(FakeXai([]), FakeFreeSound())
    with client:
        response = client.post(
            "/stories/process",
            json={"deepgram": {"transcript": "   ", "words": []}},
        )
        assert response.status_code == 422
        assert client.get("/stories").json() == []
        assert store.rows == {}


def test_missing_xai_key_fails_clearly_and_keeps_the_recording():
    store = MemoryRecordingStore()
    settings = Settings(
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-test",
        xai_api_key="",
        freesound_api_key="",
    )
    with TestClient(
        create_app(settings=settings, store=store, catalog_loader=_file_catalog)
    ) as client:
        response = client.post("/stories/process", json=_payload())
        assert response.status_code == 503
        detail = response.json()["detail"]
        assert "XAI_API_KEY" in detail["message"]
        recording_id = detail["recording_id"]
        stored = client.get(f"/stories/{recording_id}")
        assert stored.status_code == 200
        assert stored.json()["status"] == "failed"
        assert stored.json()["sfx_url"] is None
        assert client.get(f"/stories/{recording_id}/sfx", follow_redirects=False).status_code == 404


def test_missing_supabase_config_fails_before_a_row_is_written():
    settings = Settings(
        supabase_url="",
        supabase_service_role_key="",
        xai_api_key="test",
        freesound_api_key="test",
    )
    app = create_app(
        settings=settings,
        xai_client=FakeXai(
            [SfxCue(query="rain", description="rain", start=1.28, end=2.6)]
        ),
        freesound_client=FakeFreeSound(),
    )
    with TestClient(app) as client:
        response = client.post("/stories/process", json=_payload())
        assert response.status_code == 503
        assert "SUPABASE_SERVICE_ROLE_KEY" in response.json()["detail"]


def test_a_missing_freesound_hit_is_a_warning_not_a_failed_story():
    xai = FakeXai(
        [
            SfxCue(query="rain", description="rain", start=1.28, end=2.6),
            SfxCue(query="unicorn sneeze", description="no such clip", start=6.3, end=7.1),
        ]
    )
    freesound = FakeFreeSound(
        fail_queries={"unicorn sneeze": FreeSoundError("No FreeSound results for query 'unicorn sneeze'")}
    )
    client, _store = _client(xai, freesound)
    with client:
        created = client.post("/stories/process", json=_payload())
        assert created.status_code == 201
        body = created.json()
        assert body["status"] == "ready"
        assert len(body["warnings"]) == 1
        assert "unicorn sneeze" in body["warnings"][0]
        assert client.get(f"/stories/{body['id']}/sfx", follow_redirects=False).status_code == 307


def test_freesound_rate_limit_on_every_cue_is_429():
    xai = FakeXai(
        [SfxCue(query="rain", description="rain", start=1.28, end=2.6)]
    )
    freesound = FakeFreeSound(
        fail_queries={
            "rain": FreeSoundRateLimitError("FreeSound rate limit exceeded")
        }
    )
    client, _store = _client(xai, freesound)
    with client:
        response = client.post("/stories/process", json=_payload())
        assert response.status_code == 429
        recording_id = response.json()["detail"]["recording_id"]
        assert client.get(f"/stories/{recording_id}").json()["status"] == "failed"


def test_transcribe_url_returns_word_timestamps():
    deepgram = FakeDeepgram(json.loads(FIXTURE.read_text()))
    client, _store = _client(FakeXai([]), FakeFreeSound(), deepgram)
    with client:
        response = client.post("/stories/transcribe", json={"url": "https://example.test/story.wav"})
        assert response.status_code == 200
        body = response.json()
        assert body["transcript_text"].startswith("The rain began")
        assert body["duration_seconds"] == 55.78
        rain = next(word for word in body["words"] if word["word"] == "rain")
        assert rain["start"] == 0.75
        assert rain["end"] == 1.17
        assert body["segments"][0]["end"] == 4.21
        assert body["deepgram"]["metadata"]["duration"] == 55.78
    assert deepgram.urls == ["https://example.test/story.wav"]


def test_process_audio_transcribes_then_stores_sfx():
    deepgram = FakeDeepgram(json.loads(FIXTURE.read_text()))
    xai = FakeXai(
        [SfxCue(query="rain", description="rain", start=1.28, end=2.6)]
    )
    client, store = _client(xai, FakeFreeSound(), deepgram)
    with client:
        response = client.post(
            "/stories/process-audio",
            json={
                "url": "https://example.test/story.wav",
                "story_id": "story-9",
                "title": "From audio",
                "narrator": "Grandpa",
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["story_id"] == "story-9"
        assert body["title"] == "From audio"
        assert body["source_audio_url"] == "https://example.test/story.wav"
        assert body["cues"][0]["start_ms"] == 1280
        assert body["status"] == "ready"
        assert f"{body['id']}/sfx.mp3" in store.files
    assert deepgram.urls == ["https://example.test/story.wav"]
    assert xai.calls == 1


def test_process_plans_and_downloads_from_the_runtime_catalog():
    seen: dict = {}

    class PlanningXai:
        def plan_cues(self, transcript, catalog=None):
            seen["ids"] = [item["id"] for item in catalog or []]
            return [SfxCue(query="owl-hoot", description="An owl.", start=1.28, end=2.6)]

    class BindingFreeSound(FakeFreeSound):
        def __init__(self) -> None:
            super().__init__()
            self.bound = None

        def bind_catalog(self, catalog: dict):
            self.bound = catalog
            return None

    catalog = {
        "version": 3,
        "entries": [
            {
                "id": "owl-hoot",
                "label": "Owl hoot",
                "category": "animals",
                "keywords": ["owl"],
                "status": "approved",
            }
        ],
    }
    freesound = BindingFreeSound()
    app, _store = _app(PlanningXai(), freesound, catalog_loader=lambda: catalog)
    with TestClient(app) as client:
        created = client.post("/stories/process", json=_payload())
        assert created.status_code == 201
        assert created.json()["cues"][0]["query"] == "owl-hoot"
    assert seen["ids"] == ["owl-hoot"]
    assert freesound.bound["entries"][0]["id"] == "owl-hoot"
    assert freesound.queries == ["owl-hoot"]


class _CatalogXai:
    def __init__(self, blurb: StoryBlurb) -> None:
        self.blurb = blurb
        self.described = 0

    def plan_cues(self, transcript, catalog=None):
        raise AssertionError("describing a story must not plan sound effects")

    def describe_story(self, transcript):
        self.described += 1
        assert transcript.text.startswith("The rain began")
        return self.blurb


def test_describe_upload_returns_a_catalog_card_without_mixing():
    deepgram = FakeDeepgram(json.loads(FIXTURE.read_text()))
    xai = _CatalogXai(
        StoryBlurb(
            description="  Rain taps the roof while a family stays inside.  ",
            hashtags=["Calm", "#bedtime", "marketplace", "animals", "funny"],
        )
    )
    freesound = FakeFreeSound()
    client, _store = _client(
        xai,
        freesound,
        deepgram,
        settings=_settings(deepgram_api_key="dg-test"),
    )
    with client:
        response = client.post(
            "/stories/describe",
            files={"audio": ("story.mp3", b"not-real-audio", "audio/mpeg")},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["audio"] == "story.mp3"
        assert body["duration_seconds"] == 55.78
        assert body["transcript_text"].startswith("The rain began")
        assert body["description"] == "Rain taps the roof while a family stays inside."
        assert body["hashtags"] == ["calm", "bedtime", "animals"]
        assert "cues" not in body
    assert deepgram.audio == [(len(b"not-real-audio"), "audio/mpeg")]
    assert xai.described == 1
    assert freesound.queries == []


def test_describe_url_uses_the_same_transcript():
    deepgram = FakeDeepgram(json.loads(FIXTURE.read_text()))
    xai = _CatalogXai(StoryBlurb(description="A quiet rain story.", hashtags=["calm"]))
    client, _store = _client(xai, FakeFreeSound(), deepgram)
    with client:
        response = client.post(
            "/stories/describe",
            json={"url": "https://example.test/story.mp3"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["audio"] == "https://example.test/story.mp3"
        assert body["description"] == "A quiet rain story."
        assert body["hashtags"] == ["calm"]
    assert deepgram.urls == ["https://example.test/story.mp3"]
    assert xai.described == 1


def test_cover_upload_describes_then_draws_without_mixing():
    deepgram = FakeDeepgram(json.loads(FIXTURE.read_text()))

    class PictureXai:
        def __init__(self) -> None:
            self.pictured = 0

        def plan_cues(self, transcript, catalog=None):
            raise AssertionError("a cover must not plan sound effects")

        def picture_story(self, transcript):
            self.pictured += 1
            assert transcript.text.startswith("The rain began")
            return StoryCard(
                description="Rain taps the roof while a family stays inside.",
                hashtags=["calm", "bedtime"],
                scene="A family looks out a round window at the rain.",
            )

    class FakeImagine:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def generate(self, prompt: str) -> str:
            raise AssertionError("a configured Supabase project must store the image bytes")

        def generate_bytes(self, prompt: str) -> tuple[bytes, str]:
            self.prompts.append(prompt)
            assert "Rain taps the roof" in prompt
            assert "round window" in prompt
            assert "No photoreal skin" in prompt
            return b"\xff\xd8\xff cover", "image/jpeg"

    xai = PictureXai()
    imagine = FakeImagine()
    freesound = FakeFreeSound()
    client, store = _client(
        xai,
        freesound,
        deepgram,
        settings=_settings(deepgram_api_key="dg-test"),
        imagine=imagine,
    )
    with client:
        response = client.post(
            "/stories/cover",
            files={"audio": ("story.mp3", b"not-real-audio", "audio/mpeg")},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["audio"] == "story.mp3"
        assert body["duration_seconds"] == 55.78
        assert body["description"] == "Rain taps the roof while a family stays inside."
        assert body["hashtags"] == ["calm", "bedtime"]
        assert body["scene"] == "A family looks out a round window at the rain."
        assert (
            body["image_url"]
            == "https://example.supabase.co/storage/v1/object/public/story-sfx/covers/story.jpg"
        )
        assert store.files["covers/story.jpg"] == b"\xff\xd8\xff cover"
        assert "cues" not in body
    assert deepgram.audio == [(len(b"not-real-audio"), "audio/mpeg")]
    assert xai.pictured == 1
    assert len(imagine.prompts) == 1
    assert freesound.queries == []


def test_cover_without_supabase_returns_the_temporary_image_url():
    deepgram = FakeDeepgram(json.loads(FIXTURE.read_text()))

    class PictureXai:
        def picture_story(self, transcript):
            return StoryCard(
                description="A dog barks once.",
                hashtags=["animals"],
                scene="A spotted dog in a red raincoat.",
            )

    class UrlImagine:
        def generate(self, prompt: str) -> str:
            return "https://im.example/cover.jpg"

        def generate_bytes(self, prompt: str) -> tuple[bytes, str]:
            raise AssertionError("without Supabase the temporary URL is enough")

    client, store = _client(
        PictureXai(),
        FakeFreeSound(),
        deepgram,
        settings=_settings(
            deepgram_api_key="dg-test",
            supabase_url="",
            supabase_service_role_key="",
        ),
        imagine=UrlImagine(),
    )
    with client:
        response = client.post(
            "/stories/cover",
            files={"audio": ("story.mp3", b"not-real-audio", "audio/mpeg")},
        )
        assert response.status_code == 200
        assert response.json()["image_url"] == "https://im.example/cover.jpg"
    assert store.files == {}


def test_describe_missing_xai_key_is_503():
    deepgram = FakeDeepgram(json.loads(FIXTURE.read_text()))

    class MissingKey:
        def describe_story(self, transcript):
            raise XaiNotConfiguredError("XAI_API_KEY is not set. No request was sent.")

    client, _store = _client(MissingKey(), FakeFreeSound(), deepgram)
    with client:
        response = client.post(
            "/stories/describe",
            json={"url": "https://example.test/story.mp3"},
        )
        assert response.status_code == 503
        assert "XAI_API_KEY" in response.json()["detail"]


def test_transcribe_multipart_audio_uses_bytes():
    deepgram = FakeDeepgram(json.loads(FIXTURE.read_text()))
    client, _store = _client(FakeXai([]), FakeFreeSound(), deepgram)
    with client:
        response = client.post(
            "/stories/transcribe",
            files={"audio": ("story.wav", b"RIFFfake-wav", "audio/wav")},
            data={"story_id": "ignored-for-transcribe"},
        )
        assert response.status_code == 200
        assert response.json()["words"][0]["word"] == "The"
    assert deepgram.audio == [(len(b"RIFFfake-wav"), "audio/wav")]


def _describe_client():
    deepgram = FakeDeepgram(json.loads(FIXTURE.read_text()))
    xai = _CatalogXai(StoryBlurb(description="Rain on the roof.", hashtags=["calm"]))
    client, _store = _client(xai, FakeFreeSound(), deepgram, settings=_settings(deepgram_api_key="dg-test"))
    return client, deepgram


def test_several_audio_files_are_joined_into_one_recording():
    """A story's moments arrive as repeated audio fields and are transcribed once, together."""

    client, deepgram = _describe_client()
    with client:
        response = client.post(
            "/stories/describe",
            files=[
                ("audio", ("moment-1.wav", sine_wav_bytes(600), "audio/wav")),
                ("audio", ("moment-2.wav", sine_wav_bytes(600, frequency=880), "audio/wav")),
            ],
        )
    assert response.status_code == 200
    assert response.json()["audio"] == "story.mp3"
    assert len(deepgram.audio) == 1
    assert deepgram.audio[0][1] == "audio/mpeg"


def test_an_undecodable_moment_is_named_in_a_422():
    client, deepgram = _describe_client()
    with client:
        response = client.post(
            "/stories/render",
            files=[
                ("audio", ("moment-1.wav", sine_wav_bytes(300), "audio/wav")),
                ("audio", ("moment-2.m4a", b"broken", "audio/mp4")),
            ],
        )
    assert response.status_code == 422
    assert "Audio file 2" in response.json()["detail"]
    assert deepgram.audio == []


def test_the_size_limit_covers_all_moments_together(monkeypatch):
    from app.api import routes

    part = sine_wav_bytes(300)
    monkeypatch.setattr(routes, "_MAX_AUDIO_BYTES", len(part) + 10)
    client, deepgram = _describe_client()
    with client:
        response = client.post(
            "/stories/describe",
            files=[("audio", ("a.wav", part, "audio/wav")), ("audio", ("b.wav", part, "audio/wav"))],
        )
    assert response.status_code == 413
    assert deepgram.audio == []
