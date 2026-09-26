"""Audio in, lined-up audio out.

``POST /stories/render`` transcribes the upload, asks xAI for catalog ids, downloads
only those FreeSound previews, and mixes them onto the same bytes. Gladia is
covered as the fallback transcriber. No live network calls.
"""

import io
import json

import httpx
import requests
from fastapi.testclient import TestClient
from pydub import AudioSegment

from app.config import Settings
from app.main import create_app
from app.schemas.sfx import SfxCue
from app.services.freesound import HttpFreeSoundClient
from app.services.sfx_catalog import CATALOG_PATH, load_catalog
from app.services.transcribe import transcribe_audio
from tests.fakes import MemoryRecordingStore
from tests.wavutil import sine_wav_bytes


class _Planner:
    def __init__(self) -> None:
        self.duration: float | None = None

    def plan_cues(self, transcript, catalog=None):
        self.duration = transcript.duration_seconds
        assert "barked" in transcript.text.lower()
        assert catalog is not None
        assert any(item.get("id") == "dog-bark" for item in catalog)
        return [
            SfxCue(
                catalog_id="dog-bark",
                description="The dog barks.",
                start=1.0,
                end=2.0,
            )
        ]


class _Deepgram:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.audio: list[tuple[int, str]] = []

    def transcribe_url(self, url: str) -> dict:
        raise AssertionError(f"render should send bytes, not a URL ({url})")

    def transcribe_bytes(self, audio: bytes, content_type: str) -> dict:
        self.audio.append((len(audio), content_type))
        return self.payload


def test_render_lines_the_effect_up_on_the_uploaded_audio():
    story = sine_wav_bytes(3_000, frequency=220, amplitude=0.2)
    fetched: list[str] = []

    def freesound_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert "/apiv2/search" not in url
        assert "authorization" not in {key.lower() for key in request.headers}
        assert url == _preview("dog-bark")
        fetched.append(url)
        return httpx.Response(200, content=sine_wav_bytes(2_000, frequency=1400, amplitude=0.95))

    planner = _Planner()
    deepgram = _Deepgram(_listen_payload(duration=9.0))
    store = MemoryRecordingStore()
    settings = Settings(
        deepgram_api_key="dg-test",
        xai_api_key="xai-test",
        freesound_catalog_only=True,
        sfx_catalog_path=str(CATALOG_PATH),
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-test",
    )
    with httpx.Client(transport=httpx.MockTransport(freesound_handler)) as http:
        app = create_app(
            settings=settings,
            xai_client=planner,
            freesound_client=HttpFreeSoundClient(settings, http=http),
            deepgram_client=deepgram,
            store=store,
        )
        with TestClient(app) as client:
            response = client.post(
                "/stories/render",
                files={"audio": ("story.wav", story, "audio/wav")},
            )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.headers["content-disposition"] == 'attachment; filename="story_with_sfx.mp3"'
    assert deepgram.audio == [(len(story), "audio/wav")]
    assert fetched == [_preview("dog-bark")]
    assert planner.duration == 3.0
    assert store.rows == {}
    assert float(response.headers["x-story-duration-seconds"]) == 3.0

    mixed = AudioSegment.from_mp3(io.BytesIO(response.content))
    assert abs(len(mixed) - 3_000) < 500
    quiet = mixed[200:800].rms
    bark = mixed[1200:1800].rms
    assert quiet > 80
    assert bark > quiet * 1.15


def test_render_requires_a_transcriber_key():
    settings = Settings(
        deepgram_api_key="",
        gladia_api_key="",
        xai_api_key="xai-test",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-test",
    )
    app = create_app(
        settings=settings,
        xai_client=_Planner(),
        freesound_client=_UnusedFreeSound(),
        deepgram_client=_Deepgram({}),
        store=MemoryRecordingStore(),
    )
    story = sine_wav_bytes(500, frequency=220, amplitude=0.2)
    with TestClient(app) as client:
        response = client.post(
            "/stories/render",
            files={"audio": ("story.wav", story, "audio/wav")},
        )
    assert response.status_code == 503
    assert "DEEPGRAM_API_KEY" in response.json()["detail"]


def test_gladia_fallback_becomes_the_same_transcript(monkeypatch):
    monkeypatch.setenv("GLADIA_API_KEY", "gladia-test")
    calls = {"posts": [], "gets": []}

    def post(url, **kwargs):
        calls["posts"].append(url)
        assert kwargs["headers"]["x-gladia-key"] == "gladia-test"
        if url.endswith("/upload"):
            return _Response({"audio_url": "https://api.gladia.io/file/story"})
        assert url.endswith("/pre-recorded")
        return _Response({"result_url": "https://api.gladia.io/v2/pre-recorded/job-1"})

    def get(url, **kwargs):
        calls["gets"].append(url)
        return _Response(
            {
                "status": "done",
                "result": {"transcription": {"utterances": _UTTERANCES}},
            }
        )

    monkeypatch.setattr("DeepGram.requests.post", post)
    monkeypatch.setattr("DeepGram.requests.get", get)

    class _UnusedDeepgram:
        def transcribe_url(self, url: str) -> dict:
            raise AssertionError("Gladia fallback must not call Deepgram")

        def transcribe_bytes(self, audio: bytes, content_type: str) -> dict:
            raise AssertionError("Gladia fallback must not call Deepgram")

    audio = b"not-real-audio"
    document = transcribe_audio(
        Settings(gladia_api_key="gladia-test", deepgram_api_key=""),
        _UnusedDeepgram(),
        audio,
        "audio/mp4",
        "story.m4a",
    )
    from app.schemas.deepgram import DeepgramTranscript

    transcript = DeepgramTranscript.model_validate(document).normalized()
    assert calls["posts"] == [
        "https://api.gladia.io/v2/upload",
        "https://api.gladia.io/v2/pre-recorded",
    ]
    assert calls["gets"] == ["https://api.gladia.io/v2/pre-recorded/job-1"]
    assert transcript.text == "The dog barked at the creaky door."
    assert transcript.words[1].word == "dog"
    assert transcript.words[1].start == 0.4


_UTTERANCES = [
    {
        "text": "The dog barked at the creaky door.",
        "start": 0.2,
        "end": 2.6,
        "confidence": 0.98,
        "channel": 0,
        "words": [
            {"word": "The", "start": 0.2, "end": 0.4, "confidence": 0.99},
            {"word": "dog", "start": 0.4, "end": 0.8, "confidence": 0.99},
            {"word": "barked", "start": 0.8, "end": 1.3, "confidence": 0.98},
            {"word": "at", "start": 1.3, "end": 1.5, "confidence": 0.99},
            {"word": "the", "start": 1.5, "end": 1.7, "confidence": 0.99},
            {"word": "creaky", "start": 1.7, "end": 2.1, "confidence": 0.97},
            {"word": "door.", "start": 2.1, "end": 2.6, "confidence": 0.99},
        ],
    }
]


class _UnusedFreeSound:
    def download_for_query(self, query: str):
        raise AssertionError(f"should not download {query}")


def _listen_payload(duration: float) -> dict:
    return {
        "metadata": {"duration": duration, "channels": 1, "models": ["nova-3"]},
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": "The dog barked.",
                            "words": [
                                {"word": "the", "start": 0.2, "end": 0.4, "punctuated_word": "The"},
                                {"word": "dog", "start": 0.5, "end": 0.9, "punctuated_word": "dog"},
                                {"word": "barked", "start": 1.0, "end": 1.8, "punctuated_word": "barked."},
                            ],
                        }
                    ]
                }
            ],
            "utterances": [
                {"start": 0.2, "end": 1.8, "transcript": "The dog barked.", "confidence": 0.99}
            ],
        },
    }


def _preview(slot_id: str) -> str:
    catalog = load_catalog(CATALOG_PATH)
    for entry in catalog["entries"]:
        if entry["id"] == slot_id:
            return entry["preview_url"]
    raise AssertionError(f"{slot_id} has no preview_url in the catalog")


class _Response:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)
