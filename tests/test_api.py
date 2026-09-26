"""Story API with xAI and FreeSound replaced by in-process fakes."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.schemas.sfx import SfxCue
from app.services.freesound import (
    DownloadedClip,
    FreeSoundError,
    FreeSoundRateLimitError,
)
from tests.wavutil import sine_wav_bytes

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "deepgram_sample.json"


class FakeXai:
    def __init__(self, cues: list[SfxCue]) -> None:
        self.cues = cues
        self.calls = 0

    def plan_cues(self, transcript):
        self.calls += 1
        assert "rain" in transcript.text
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


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'stories.db'}",
        media_dir=str(tmp_path / "media"),
        xai_api_key="test",
        freesound_api_key="test",
    )


def _client(tmp_path: Path, xai, freesound) -> TestClient:
    app = create_app(settings=_settings(tmp_path), xai_client=xai, freesound_client=freesound)
    return TestClient(app)


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


def test_health(tmp_path):
    with _client(tmp_path, FakeXai([]), FakeFreeSound()) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_process_stores_recording_and_serves_aligned_sfx(tmp_path):
    xai = FakeXai(
        [
            SfxCue(
                query="gentle rain ambience",
                description="Rain under the first sentence.",
                start=1.28,
                end=2.6,
            ),
            SfxCue(
                query="wooden door creak",
                description="The creaky door.",
                start=4.55,
                end=5.5,
            ),
        ]
    )
    freesound = FakeFreeSound()
    with _client(tmp_path, xai, freesound) as client:
        created = client.post("/stories/process", json=_payload())
        assert created.status_code == 201
        body = created.json()
        assert body["status"] == "ready"
        assert body["story_id"] == "story-1"
        assert body["narrator"] == "Grandma"
        assert body["duration_seconds"] == 8.5
        assert body["transcript_text"].startswith("Once upon a time")
        assert [cue["start_ms"] for cue in body["cues"]] == [1280, 4550]
        assert [cue["end_ms"] for cue in body["cues"]] == [2600, 5500]
        assert body["warnings"] == []
        assert body["sfx_url"].endswith(f"/stories/{body['id']}/sfx")

        listed = client.get("/stories")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [body["id"]]
        assert listed.json()[0]["sfx_url"] == body["sfx_url"]

        filtered = client.get("/stories", params={"story_id": "other"})
        assert filtered.json() == []

        detail = client.get(f"/stories/{body['id']}")
        assert detail.status_code == 200
        assert detail.json()["transcript_json"]["metadata"]["duration"] == 8.5
        assert detail.json()["source_audio_url"] == "https://example.test/original.wav"

        sfx = client.get(f"/stories/{body['id']}/sfx")
        assert sfx.status_code == 200
        assert sfx.headers["content-type"].startswith("audio/mpeg")
        assert len(sfx.content) > 500
        assert sfx.content[:3] == b"ID3" or sfx.content[0] == 0xFF

        missing = client.get("/stories/does-not-exist")
        assert missing.status_code == 404
        assert client.get("/stories/does-not-exist/sfx").status_code == 404

    assert xai.calls == 1
    assert freesound.queries == ["gentle rain ambience", "wooden door creak"]
    stored = tmp_path / "media" / body["id"] / "sfx.mp3"
    assert stored.is_file()


def test_raw_deepgram_body_is_accepted(tmp_path):
    xai = FakeXai(
        [SfxCue(query="bird song", description="The bird.", start=6.3, end=7.6)]
    )
    with _client(tmp_path, xai, FakeFreeSound()) as client:
        created = client.post("/stories/process", json=json.loads(FIXTURE.read_text()))
        assert created.status_code == 201
        assert created.json()["story_id"] is None
        assert created.json()["cues"][0]["query"] == "bird song"


def test_invalid_transcript_is_422_and_stores_nothing(tmp_path):
    with _client(tmp_path, FakeXai([]), FakeFreeSound()) as client:
        response = client.post(
            "/stories/process",
            json={"deepgram": {"transcript": "   ", "words": []}},
        )
        assert response.status_code == 422
        assert client.get("/stories").json() == []


def test_missing_xai_key_fails_clearly_and_keeps_the_recording(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'stories.db'}",
        media_dir=str(tmp_path / "media"),
        xai_api_key="",
        freesound_api_key="",
    )
    with TestClient(create_app(settings=settings)) as client:
        response = client.post("/stories/process", json=_payload())
        assert response.status_code == 503
        detail = response.json()["detail"]
        assert "XAI_API_KEY" in detail["message"]
        recording_id = detail["recording_id"]
        stored = client.get(f"/stories/{recording_id}")
        assert stored.status_code == 200
        assert stored.json()["status"] == "failed"
        assert stored.json()["sfx_url"] is None
        assert client.get(f"/stories/{recording_id}/sfx").status_code == 404


def test_a_missing_freesound_hit_is_a_warning_not_a_failed_story(tmp_path):
    xai = FakeXai(
        [
            SfxCue(query="gentle rain ambience", description="rain", start=1.28, end=2.6),
            SfxCue(query="unicorn sneeze", description="no such clip", start=6.3, end=7.1),
        ]
    )
    freesound = FakeFreeSound(
        fail_queries={"unicorn sneeze": FreeSoundError("No FreeSound results for query 'unicorn sneeze'")}
    )
    with _client(tmp_path, xai, freesound) as client:
        created = client.post("/stories/process", json=_payload())
        assert created.status_code == 201
        body = created.json()
        assert body["status"] == "ready"
        assert len(body["warnings"]) == 1
        assert "unicorn sneeze" in body["warnings"][0]
        assert client.get(f"/stories/{body['id']}/sfx").status_code == 200


def test_freesound_rate_limit_on_every_cue_is_429(tmp_path):
    xai = FakeXai(
        [SfxCue(query="gentle rain ambience", description="rain", start=1.28, end=2.6)]
    )
    freesound = FakeFreeSound(
        fail_queries={
            "gentle rain ambience": FreeSoundRateLimitError("FreeSound rate limit exceeded")
        }
    )
    with _client(tmp_path, xai, freesound) as client:
        response = client.post("/stories/process", json=_payload())
        assert response.status_code == 429
        recording_id = response.json()["detail"]["recording_id"]
        assert client.get(f"/stories/{recording_id}").json()["status"] == "failed"
