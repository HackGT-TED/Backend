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

# Gaps at least this long are listed for the model. Shorter gaps stay unspoken.
LONG_PAUSE_MS = 600

_SYSTEM_PROMPT = """You design an immersive sound bed for a children's story.
Python downloads clips from a fixed catalog and mixes them on the story clock. You do not fetch audio.
Choose which catalog sound fits, how long it should last, and what else may play at the same time.

Rules:
- catalog_id must be copied exactly from catalog in the user message. Never invent an id.
- query is that same id, or null. It is not a search phrase.
- kind is the role of the cue. Use null only when you mean oneshot.
  - oneshot: a short event on the words that cause it (door creak, bark, footstep, chime, a thunder crack).
  - ambient: a scene bed that keeps going. Rain, wind, crickets, or room tone should cover the whole span where that scene is still true, not a split-second hit on one word. Set start when the scene becomes audible and end when the context clearly changes (they go inside, the weather stops, the story moves on). Set end_at_scene_change true when that end is a scene boundary. If the bed should hold through the rest of the story, set end_at_scene_change true and set both end and until_seconds to null.
  - fill_pause: a soft bed across one pause from the pauses list, and only when silence would feel empty while the scene still wants sound. Do not fill a pause that should stay quiet (a held breath, a reveal, the last moment before sleep).
- Pauses of at least {long_pause_ms} ms are in pauses, including trailing silence. Shorter gaps are omitted. Leave a listed pause with no cue when the quiet is the point.
- Overlap is expected. Keep an ambient bed playing and add oneshots on top at the same timestamps (rain under a door creak). Do not drop a bed to make room for an event.
- gain_db is the level for the whole cue, in decibels. Null is unity. A bed that should sit under the story is usually between -6 and -12. Do not set a gain above 0.
- until_seconds is an alternate end clock. Leave it null when end is set. Leave end null when until_seconds is set.
- reason is one short sentence.
- Return between 0 and 12 cues. Prefer one ambient cue for a whole scene instead of repeating a short rain hit. Zero cues is allowed.
- Times are seconds. start must be >= 0. When end is a number it must be <= duration_seconds and greater than start.
"""


class XaiClient(Protocol):
    """Plans timed sound-effect cues for one transcript."""

    def plan_cues(
        self,
        transcript: NormalizedTranscript,
        catalog: list[dict] | None = None,
    ) -> list[SfxCue]:
        """Return catalog sounds and times for ``transcript``."""


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

    def plan_cues(
        self,
        transcript: NormalizedTranscript,
        catalog: list[dict] | None = None,
    ) -> list[SfxCue]:
        if not self._api_key:
            raise XaiNotConfiguredError(
                "XAI_API_KEY is not set. Add it to .env (see .env.example). "
                "No request was sent."
            )
        plan = self._complete(transcript, catalog)
        return align_cues(plan.cues, transcript.duration_seconds)

    def _complete(self, transcript: NormalizedTranscript, catalog: list[dict] | None) -> SfxPlan:
        user = planning_user_payload(transcript, catalog)
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt()},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
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


def system_prompt() -> str:
    """Planner instructions, including the pause threshold the user payload uses."""

    return _SYSTEM_PROMPT.format(long_pause_ms=LONG_PAUSE_MS)


def planning_user_payload(
    transcript: NormalizedTranscript,
    catalog: list[dict] | None = None,
) -> dict:
    """Compact story clock plus catalog ids.

    Word timings, segments, and long pauses only. This is not a Deepgram
    document: no confidence, speaker, channels, or metadata.
    """

    compact = transcript.prompt_payload()
    compact["pauses"] = transcript.pause_gaps(LONG_PAUSE_MS)
    compact["long_pause_ms"] = LONG_PAUSE_MS
    compact["catalog"] = catalog_for_prompt(catalog)
    return compact


def catalog_for_prompt(catalog: list[dict] | None) -> list[dict]:
    """Id and label only, even if the caller passed a full catalog row."""

    slim: list[dict] = []
    for item in catalog or []:
        if not isinstance(item, dict):
            continue
        slot_id = item.get("id") or item.get("catalog_id")
        if not slot_id:
            continue
        entry = {"id": str(slot_id)}
        label = item.get("label")
        if label:
            entry["label"] = str(label)
        slim.append(entry)
    return slim


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
