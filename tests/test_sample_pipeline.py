"""Sample wiring: transcription -> xAI catalog ids -> those FreeSound previews -> MP3.

External APIs are mocked. The test still runs the real modules: ``DeepGram.py``
(Gladia upload + poll), the Deepgram listen client, xAI chat completions, the
checked-in catalog, and the mixer.
"""

import base64
import json

import httpx
import requests
from fastapi.testclient import TestClient

from app.config import Settings
from app.schemas.deepgram import DeepgramTranscript
from app.services.deepgram import HttpDeepgramClient
from app.services.freesound import HttpFreeSoundClient
from app.services.gladia_transcript import normalize_gladia
from app.services.pipeline import run_pipeline
from app.services.sfx_catalog import CATALOG_PATH, load_catalog
from app.services.xai import HttpXaiClient
from tests.wavutil import sine_wav_bytes

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


def test_gladia_transcribe_then_mixes_only_the_catalog_sounds(monkeypatch):
    monkeypatch.setenv("GLADIA_API_KEY", "gladia-test")
    calls = {"posts": [], "gets": []}

    def post(url, **kwargs):
        calls["posts"].append(url)
        assert kwargs["headers"]["x-gladia-key"] == "gladia-test"
        if url.endswith("/upload"):
            return _Response({"audio_url": "https://api.gladia.io/file/story"})
        assert url.endswith("/pre-recorded")
        body = kwargs["json"]
        assert body["audio_url"] == "https://api.gladia.io/file/story"
        return _Response({"result_url": "https://api.gladia.io/v2/pre-recorded/job-1"})

    def get(url, **kwargs):
        calls["gets"].append(url)
        assert kwargs["headers"]["x-gladia-key"] == "gladia-test"
        return _Response(
            {
                "status": "done",
                "result": {"transcription": {"utterances": _UTTERANCES}},
            }
        )

    monkeypatch.setattr("DeepGram.requests.post", post)
    monkeypatch.setattr("DeepGram.requests.get", get)

    from main import app

    audio = base64.b64encode(b"not-real-audio").decode("ascii")
    with TestClient(app) as client:
        response = client.post(
            "/transcribe",
            json={
                "filename": "story.m4a",
                "content_type": "audio/mp4",
                "audio_base64": audio,
                "size_bytes": 14,
            },
        )
    assert response.status_code == 200
    utterances = response.json()
    assert isinstance(utterances, list)
    assert calls["posts"] == [
        "https://api.gladia.io/v2/upload",
        "https://api.gladia.io/v2/pre-recorded",
    ]
    assert calls["gets"] == ["https://api.gladia.io/v2/pre-recorded/job-1"]

    transcript = normalize_gladia(utterances)
    assert transcript.text == "The dog barked at the creaky door."
    assert transcript.words[1].word == "dog"
    assert transcript.words[1].start == 0.4
    assert transcript.duration_seconds == 2.6

    output, fetched = _mix_with_mocked_xai_and_catalog(transcript)
    assert fetched == [_preview("dog-bark"), _preview("door-creak")]
    assert output.duration_seconds == 2.6
    assert [cue.catalog_id for cue in output.cues] == ["dog-bark", "door-creak"]
    assert output.audio_bytes[:3] == b"ID3" or output.audio_bytes[0] == 0xFF


def test_deepgram_listen_json_uses_the_same_mix():
    payload = {
        "metadata": {"duration": 3.0, "channels": 1, "models": ["nova-3"]},
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": "The dog barked.",
                            "words": [
                                {"word": "the", "start": 0.1, "end": 0.3, "punctuated_word": "The"},
                                {"word": "dog", "start": 0.3, "end": 0.7, "punctuated_word": "dog"},
                                {"word": "barked", "start": 0.7, "end": 1.2, "punctuated_word": "barked."},
                            ],
                        }
                    ]
                }
            ],
            "utterances": [
                {"start": 0.1, "end": 1.2, "transcript": "The dog barked.", "confidence": 0.99}
            ],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/listen"
        assert request.headers["authorization"] == "Token dg-key"
        assert json.loads(request.content) == {"url": "https://example.test/story.m4a"}
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        listened = HttpDeepgramClient(
            Settings(deepgram_api_key="dg-key", deepgram_base_url="https://api.deepgram.com"),
            http=http,
        ).transcribe_url("https://example.test/story.m4a")

    transcript = DeepgramTranscript.model_validate(listened).normalized()
    output, fetched = _mix_with_mocked_xai_and_catalog(transcript)
    assert fetched == [_preview("dog-bark")]
    assert output.duration_seconds == 3.0
    assert output.cues[0].catalog_id == "dog-bark"


def _mix_with_mocked_xai_and_catalog(transcript):
    fetched: list[str] = []

    def xai_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["authorization"] == "Bearer xai-test"
        body = json.loads(request.content)
        user = json.loads(body["messages"][1]["content"])
        ids = {item["id"] for item in user["catalog"]}
        assert {"dog-bark", "door-creak"} <= ids
        cues = []
        text = user["transcript"].lower()
        if "dog" in text or "bark" in text:
            cues.append(
                {
                    "catalog_id": "dog-bark",
                    "description": "The dog barks.",
                    "start": 0.4,
                    "end": 1.3,
                }
            )
        if "door" in text:
            cues.append(
                {
                    "catalog_id": "door-creak",
                    "description": "The door.",
                    "start": 1.7,
                    "end": 2.6,
                }
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"cues": cues})}}]},
        )

    allowed = {_preview("dog-bark"), _preview("door-creak")}

    def freesound_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert "/apiv2/search" not in url
        assert "authorization" not in {key.lower() for key in request.headers}
        assert url in allowed
        fetched.append(url)
        return httpx.Response(200, content=sine_wav_bytes(400, frequency=523))

    xai_settings = Settings(xai_api_key="xai-test", xai_base_url="https://api.x.ai/v1", xai_model="grok-4.7")
    fs_settings = Settings(freesound_catalog_only=True, sfx_catalog_path=str(CATALOG_PATH), freesound_api_key="")
    with httpx.Client(transport=httpx.MockTransport(xai_handler)) as xai_http, httpx.Client(
        transport=httpx.MockTransport(freesound_handler)
    ) as fs_http:
        output = run_pipeline(
            transcript,
            HttpXaiClient(xai_settings, http=xai_http),
            HttpFreeSoundClient(fs_settings, http=fs_http),
        )
    return output, fetched


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
