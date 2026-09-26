"""Muse Spark client.

Muse Spark is served by Meta's Model API. Chat Completions are OpenAI-compatible:

* Base URL: ``https://api.meta.ai/v1`` (``MUSE_SPARK_BASE_URL``)
* Endpoint: ``POST /chat/completions``
* Model: ``muse-spark-1.3`` (``MUSE_SPARK_MODEL``)
* Auth: ``Authorization: Bearer $MUSE_SPARK_API_KEY``

Meta's own docs call the credential ``MODEL_API_KEY``. This service reads
``MUSE_SPARK_API_KEY`` so the teddy-bear project has one obvious env var.
Structured cues use ``response_format`` with ``type: json_schema`` (Chat
Completions). The Responses API parameter ``text.format`` is a different
endpoint and is not sent here.

Docs: https://ai.developer.meta.com/docs/protocols/chat-completions
and https://ai.developer.meta.com/docs/structured-output
"""

import json
from typing import Protocol

import httpx

from app.config import Settings
from app.schemas.deepgram import NormalizedTranscript
from app.schemas.sfx import SFX_RESPONSE_FORMAT, SfxCue, SfxPlan, align_cues

_SYSTEM_PROMPT = """You are the sound designer for a children's teddy bear that plays a grandparent's story.
Given a transcript with word and segment timestamps, choose sound effects that match the story.
Rules:
- Return between 0 and 8 cues. Use fewer cues when the story is short. Zero cues is allowed when nothing in the story wants an effect.
- Align start and end to the words the effect should accompany. Times are seconds from the start of the story.
- start must be >= 0 and end must be <= duration_seconds. end must be greater than start.
- query is a short FreeSound search (2-6 words) such as "gentle rain ambience" or "wooden door creak".
- Prefer ambience, foley, and nature sounds suitable for young children. Do not request speech or songs with lyrics.
- description is one sentence explaining the cue.
"""


class MuseSparkClient(Protocol):
    """Plans timed sound-effect cues for one transcript."""

    def plan_cues(self, transcript: NormalizedTranscript) -> list[SfxCue]:
        """Return cues aligned to ``transcript.duration_seconds``."""


class MuseSparkError(RuntimeError):
    """The Muse Spark call failed or returned a payload we could not use."""


class MuseSparkNotConfiguredError(MuseSparkError):
    """``MUSE_SPARK_API_KEY`` is empty, so no request was sent."""


class MuseSparkAuthError(MuseSparkError):
    """The Model API rejected the configured key."""


class HttpMuseSparkClient:
    """Chat Completions client. A missing key fails before any HTTP call."""

    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self._api_key = settings.muse_spark_api_key.strip()
        self._model = settings.muse_spark_model
        self._url = settings.muse_spark_base_url.rstrip("/") + "/chat/completions"
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=settings.http_timeout_seconds)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def plan_cues(self, transcript: NormalizedTranscript) -> list[SfxCue]:
        if not self._api_key:
            raise MuseSparkNotConfiguredError(
                "MUSE_SPARK_API_KEY is not set. Add it to .env (see .env.example). "
                "Meta Model API docs call this credential MODEL_API_KEY; this service "
                "reads MUSE_SPARK_API_KEY. No request was sent."
            )
        plan = self._complete(transcript)
        return align_cues(plan.cues, transcript.duration_seconds)

    def _complete(self, transcript: NormalizedTranscript) -> SfxPlan:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(transcript.prompt_payload(), ensure_ascii=False),
                },
            ],
            "response_format": SFX_RESPONSE_FORMAT,
        }
        try:
            response = self._http.post(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise MuseSparkError(f"Muse Spark request failed: {exc}") from exc

        if response.status_code in {401, 403}:
            raise MuseSparkAuthError(
                f"Muse Spark rejected MUSE_SPARK_API_KEY (HTTP {response.status_code})."
            )
        if response.status_code >= 400:
            detail = response.text[:500]
            raise MuseSparkError(
                f"Muse Spark request failed (HTTP {response.status_code}): {detail}"
            )

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise MuseSparkError(f"Unexpected Muse Spark response shape: {exc}") from exc

        text = _message_text(content)
        if not text.strip():
            raise MuseSparkError("Muse Spark returned an empty completion")
        try:
            return SfxPlan.model_validate_json(_strip_fences(text))
        except ValueError as exc:
            raise MuseSparkError(f"Muse Spark cue JSON did not match the SFX schema: {exc}") from exc


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
        return "".join(parts)
    raise MuseSparkError("Muse Spark message content was not text")


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    stripped = stripped.split("\n", 1)[-1] if "\n" in stripped else stripped
    if stripped.endswith("```"):
        stripped = stripped[: stripped.rfind("```")]
    return stripped.strip()
