"""Turn an uploaded recording into the Deepgram-shaped JSON the mixer already uses.

Deepgram ``POST /v1/listen`` is used when ``DEEPGRAM_API_KEY`` is set. Otherwise
the file goes through ``DeepGram.py`` (Gladia upload + poll) and the utterances
are converted to that same document. Word times from either source are what
Python uses to place catalog clips.
"""

from __future__ import annotations

import base64
import json

import requests

from app.config import Settings
from app.services.deepgram import DeepgramClient
from app.services.gladia_transcript import gladia_to_listen_document


class TranscriptionError(RuntimeError):
    """Transcription failed or returned a payload we could not use."""


class TranscriptionNotConfiguredError(TranscriptionError):
    """Neither Deepgram nor Gladia is configured, so no request was sent."""


def transcribe_audio(
    settings: Settings,
    deepgram: DeepgramClient,
    audio: bytes,
    content_type: str,
    filename: str,
) -> dict:
    """Transcribe ``audio``. Deepgram wins when both keys are set."""

    if settings.deepgram_api_key.strip():
        return deepgram.transcribe_bytes(audio, content_type)
    if settings.gladia_api_key.strip():
        return _transcribe_gladia(audio, content_type, filename)
    raise TranscriptionNotConfiguredError(
        "Set DEEPGRAM_API_KEY or GLADIA_API_KEY to transcribe an upload. "
        "No request was sent."
    )


def _transcribe_gladia(audio: bytes, content_type: str, filename: str) -> dict:
    from DeepGram import fullDeepGramPipeline

    payload = json.dumps(
        {
            "filename": filename or "story.audio",
            "content_type": content_type or "application/octet-stream",
            "size_bytes": len(audio),
            "audio_base64": base64.b64encode(audio).decode("ascii"),
        }
    )
    try:
        raw = fullDeepGramPipeline(payload)
    except RuntimeError as exc:
        message = str(exc)
        if "GLADIA_API_KEY" in message:
            raise TranscriptionNotConfiguredError(message) from exc
        raise TranscriptionError(message) from exc
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        raise TranscriptionError(f"Gladia error: {detail}") from exc
    try:
        return gladia_to_listen_document(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise TranscriptionError(f"Gladia transcript could not be read: {exc}") from exc
