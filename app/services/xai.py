"""xAI chat client for sound-effect planning.

xAI's Chat Completions API is OpenAI-compatible:

* Base URL: ``https://api.x.ai/v1`` (``XAI_BASE_URL``)
* Endpoint: ``POST /chat/completions``
* Model: ``grok-4.7`` (``XAI_MODEL``)
* Auth: ``Authorization: Bearer $XAI_API_KEY``

``grok-4.7`` is the chat model xAI documents as the default for text work, and
its model page lists structured outputs as supported. Cues are requested with
``response_format.type = "json_schema"``. The assistant message is still parsed
defensively: fenced JSON and a JSON object wrapped in prose both work.

Docs: https://docs.x.ai/docs/models/grok-4.7
and https://docs.x.ai/developers/model-capabilities/text/structured-outputs
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


class XaiClient(Protocol):
    """Plans timed sound-effect cues for one transcript."""

    def plan_cues(self, transcript: NormalizedTranscript) -> list[SfxCue]:
        """Return cues aligned to ``transcript.duration_seconds``."""


class XaiError(RuntimeError):
    """The xAI call failed or returned a payload we could not use."""


class XaiNotConfiguredError(XaiError):
    """``XAI_API_KEY`` is empty, so no request was sent."""


class XaiAuthError(XaiError):
    """xAI rejected the configured key."""


class HttpXaiClient:
    """Chat Completions client. A missing key fails before any HTTP call."""

    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self._api_key = settings.xai_api_key.strip()
        self._model = settings.xai_model
        self._url = settings.xai_base_url.rstrip("/") + "/chat/completions"
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=settings.http_timeout_seconds)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def plan_cues(self, transcript: NormalizedTranscript) -> list[SfxCue]:
        if not self._api_key:
            raise XaiNotConfiguredError(
                "XAI_API_KEY is not set. Add it to .env (see .env.example). "
                "No request was sent."
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
            raise XaiError(f"xAI request failed: {exc}") from exc

        if response.status_code in {401, 403}:
            raise XaiAuthError(
                f"xAI rejected XAI_API_KEY (HTTP {response.status_code})."
            )
        if response.status_code >= 400:
            detail = response.text[:500]
            raise XaiError(f"xAI request failed (HTTP {response.status_code}): {detail}")

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise XaiError(f"Unexpected xAI response shape: {exc}") from exc

        text = _message_text(content)
        if not text.strip():
            raise XaiError("xAI returned an empty completion")
        try:
            return _parse_plan(text)
        except ValueError as exc:
            raise XaiError(f"xAI cue JSON did not match the SFX schema: {exc}") from exc


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
    raise XaiError("xAI message content was not text")


def _parse_plan(text: str) -> SfxPlan:
    cleaned = _strip_fences(text)
    try:
        return SfxPlan.model_validate_json(cleaned)
    except ValueError:
        extracted = _extract_json_object(cleaned)
        if extracted is None:
            raise
        return SfxPlan.model_validate_json(extracted)


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    stripped = stripped.split("\n", 1)[-1] if "\n" in stripped else stripped
    if stripped.endswith("```"):
        stripped = stripped[: stripped.rfind("```")]
    return stripped.strip()


def _extract_json_object(text: str) -> str | None:
    """Return the first balanced JSON object, ignoring surrounding prose."""

    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None
