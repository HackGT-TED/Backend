"""Deepgram prerecorded transcription.

``POST {DEEPGRAM_BASE_URL}/v1/listen`` with ``Authorization: Token $DEEPGRAM_API_KEY``.
The default model is ``nova-3``. The JSON body is the same shape the SFX pipeline
already accepts, including word and utterance timestamps.

https://developers.deepgram.com/docs/pre-recorded-audio
"""

from typing import Protocol

import httpx

from app.config import Settings

_LISTEN_PATH = "/v1/listen"


class DeepgramClient(Protocol):
    def transcribe_url(self, url: str) -> dict:
        """Transcribe audio Deepgram can fetch from ``url``."""

    def transcribe_bytes(self, audio: bytes, content_type: str) -> dict:
        """Transcribe raw audio bytes."""


class DeepgramError(RuntimeError):
    """The Deepgram call failed or returned a payload we could not use."""


class DeepgramNotConfiguredError(DeepgramError):
    """``DEEPGRAM_API_KEY`` is empty, so no request was sent."""


class DeepgramAuthError(DeepgramError):
    """Deepgram rejected the configured key."""


class HttpDeepgramClient:
    """Prerecorded listen client. A missing key fails before any HTTP call."""

    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self._api_key = settings.deepgram_api_key.strip()
        self._model = settings.deepgram_model
        self._language = settings.deepgram_language
        self._url = settings.deepgram_base_url.rstrip("/") + _LISTEN_PATH
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=settings.http_timeout_seconds)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def transcribe_url(self, url: str) -> dict:
        cleaned = url.strip()
        if not cleaned:
            raise DeepgramError("Audio URL is empty")
        return self._listen(json_body={"url": cleaned}, content=None, content_type="application/json")

    def transcribe_bytes(self, audio: bytes, content_type: str) -> dict:
        if not audio:
            raise DeepgramError("Audio payload was empty")
        mime = content_type.strip() or "application/octet-stream"
        return self._listen(json_body=None, content=audio, content_type=mime)

    def _listen(
        self,
        *,
        json_body: dict | None,
        content: bytes | None,
        content_type: str,
    ) -> dict:
        if not self._api_key:
            raise DeepgramNotConfiguredError(
                "DEEPGRAM_API_KEY is not set. Add it to .env (see .env.example). "
                "No request was sent."
            )
        request_kwargs: dict = {}
        if json_body is not None:
            request_kwargs["json"] = json_body
        else:
            request_kwargs["content"] = content
        try:
            response = self._http.post(
                self._url,
                params={
                    "model": self._model,
                    "language": self._language,
                    "smart_format": "true",
                    "punctuate": "true",
                    "utterances": "true",
                    "paragraphs": "true",
                },
                headers={
                    "Authorization": f"Token {self._api_key}",
                    "Content-Type": content_type,
                },
                **request_kwargs,
            )
        except httpx.HTTPError as exc:
            raise DeepgramError(f"Deepgram request failed: {exc}") from exc

        if response.status_code in {401, 403}:
            raise DeepgramAuthError(
                f"Deepgram rejected DEEPGRAM_API_KEY (HTTP {response.status_code})."
            )
        if response.status_code >= 400:
            detail = response.text[:500]
            raise DeepgramError(f"Deepgram request failed (HTTP {response.status_code}): {detail}")
        try:
            body = response.json()
        except ValueError as exc:
            raise DeepgramError("Deepgram returned non-JSON") from exc
        if not isinstance(body, dict):
            raise DeepgramError("Deepgram response was not a JSON object")
        return body
