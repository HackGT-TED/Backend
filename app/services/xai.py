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
from app.schemas.api import STORY_HASHTAGS, StoryBlurb, StoryCard
from app.schemas.deepgram import NormalizedTranscript
from app.schemas.sfx import (
    SFX_RESPONSE_FORMAT,
    SfxCue,
    SfxPlan,
    align_cues,
    limit_to_one_cue_per_sentence,
)

_SYSTEM_PROMPT = """You choose sound effects for a children's story from a fixed catalog.
Python downloads those clips and places them on the timeline. You do not fetch audio.

Be sparse. The narration should stay easy to follow, so most of the story has no effect.
- At most one cue per sentence. A sentence is one entry in "segments", or the words up to the next period, question mark, or exclamation mark.
- Most sentences get zero cues. Add one only when that sentence plainly names the sound (rain, a bark, a door, thunder, a kettle, footsteps). If the match is vague or decorative, skip it.
- Do not give a sentence a second effect. If two catalog rows could fit, keep the single closest one.
- Do not reuse a sound on later sentences just because an earlier sentence mentioned it.
- Zero cues is the right answer when nothing in the catalog is an obvious match.
- catalog_id must be copied exactly from the catalog list in the user message. Never invent an id.
- start is the start time, in seconds, of the word this sound belongs to. end is when that sound should stop.
- Keep a short event (a bark, a creak, a chime) inside the same sentence. A continuing sound such as rain may run until it would fade, still within duration_seconds.
- start must be >= 0 and end must be <= duration_seconds. end must be greater than start.
- description is one short sentence naming the word you matched.
"""

_SUMMARY_PROMPT = """You write a catalog card for one children's story recording.
You are given the transcript from speech-to-text. You do not choose sound effects and you do not time anything.

- description is one sentence a parent could scan in a list of stories.
- hashtags are 1 to 3 tags, and each tag is copied from this list only: spooky, calm, funny, adventure, bedtime, animals, nature, family, magic.
- Pick the tags that actually fit the story. Leave the others out.
"""

SUMMARY_RESPONSE_FORMAT: dict = {
    "type": "json_schema",
    "json_schema": {
        "name": "story_summary",
        "description": "One-sentence catalog description and a few hashtags.",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "description": {"type": "string"},
                "hashtags": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(STORY_HASHTAGS)},
                },
            },
            "required": ["description", "hashtags"],
        },
    },
}

_PICTURE_PROMPT = """You write a catalog card for one children's story recording, plus one picture moment.
You are given the transcript from speech-to-text. You do not choose sound effects and you do not time anything.

- description is one sentence a parent could scan in a list of stories.
- hashtags are 1 to 3 tags, and each tag is copied from this list only: spooky, calm, funny, adventure, bedtime, animals, nature, family, magic.
- Pick the tags that actually fit the story. Leave the others out.
- scene is one sentence a child could point at: who is in the picture, where they are, and what they are doing. Use details from the transcript. No sound-effect words, no camera words, no style words.
"""

PICTURE_RESPONSE_FORMAT: dict = {
    "type": "json_schema",
    "json_schema": {
        "name": "story_picture",
        "description": "Catalog description, hashtags, and one picture-book moment.",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "description": {"type": "string"},
                "hashtags": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(STORY_HASHTAGS)},
                },
                "scene": {"type": "string"},
            },
            "required": ["description", "hashtags", "scene"],
        },
    },
}


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


# grok-4.7 reasons before it returns JSON. A one-minute read limit drops
# cue planning on a full story. Downloads keep the shorter HTTP_TIMEOUT_SECONDS.
_XAI_READ_TIMEOUT_FLOOR_SECONDS = 180.0


def xai_timeout(settings: Settings) -> httpx.Timeout:
    """Read timeout for Chat Completions. At least three minutes."""

    read_seconds = max(float(settings.http_timeout_seconds), _XAI_READ_TIMEOUT_FLOOR_SECONDS)
    return httpx.Timeout(connect=30.0, read=read_seconds, write=60.0, pool=30.0)


class HttpXaiClient:
    """Chat Completions client. A missing key fails before any HTTP call."""

    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self._api_key = settings.xai_api_key.strip()
        self._model = settings.xai_model
        self._url = settings.xai_base_url.rstrip("/") + "/chat/completions"
        self._timeout = xai_timeout(settings)
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=self._timeout)

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
        aligned = align_cues(plan.cues, transcript.duration_seconds)
        return limit_to_one_cue_per_sentence(aligned, transcript.sentence_windows())

    def describe_story(self, transcript: NormalizedTranscript) -> StoryBlurb:
        """One-sentence catalog description from the transcript. Does not plan cues."""

        if not self._api_key:
            raise XaiNotConfiguredError(
                "XAI_API_KEY is not set. Add it to .env (see .env.example). "
                "No request was sent."
            )
        if not transcript.text.strip():
            raise XaiError("Transcript was empty, so no description was requested.")
        text = self._post_completion(
            [
                {"role": "system", "content": _SUMMARY_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "transcript": transcript.text,
                            "duration_seconds": transcript.duration_seconds,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            SUMMARY_RESPONSE_FORMAT,
        )
        try:
            return _parse_summary(text)
        except ValueError as exc:
            raise XaiError(f"xAI summary JSON could not be read: {exc}") from exc

    def picture_story(self, transcript: NormalizedTranscript) -> StoryCard:
        """Catalog card plus one visual moment. Does not plan cues or draw the image."""

        if not self._api_key:
            raise XaiNotConfiguredError(
                "XAI_API_KEY is not set. Add it to .env (see .env.example). "
                "No request was sent."
            )
        if not transcript.text.strip():
            raise XaiError("Transcript was empty, so no description was requested.")
        text = self._post_completion(
            [
                {"role": "system", "content": _PICTURE_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "transcript": transcript.text,
                            "duration_seconds": transcript.duration_seconds,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            PICTURE_RESPONSE_FORMAT,
            reasoning_effort="low",
        )
        try:
            return _parse_picture(text)
        except ValueError as exc:
            raise XaiError(f"xAI picture JSON could not be read: {exc}") from exc

    def _complete(self, transcript: NormalizedTranscript, catalog: list[dict] | None) -> SfxPlan:
        user = transcript.prompt_payload()
        user["catalog"] = catalog or []
        text = self._post_completion(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            SFX_RESPONSE_FORMAT,
        )
        try:
            return _parse_plan(text)
        except ValueError as exc:
            raise XaiError(f"xAI cue JSON did not match the SFX schema: {exc}") from exc

    def _post_completion(
        self,
        messages: list[dict],
        response_format: dict,
        *,
        reasoning_effort: str | None = None,
    ) -> str:
        payload = {
            "model": self._model,
            "messages": messages,
            "response_format": response_format,
        }
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        try:
            response = self._http.post(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except httpx.TimeoutException as exc:
            raise XaiError(
                f"xAI did not answer within {self._timeout.read:.0f}s. "
                "Cue planning can take a few minutes. "
                "Set HTTP_TIMEOUT_SECONDS higher than 180 and run the same command again."
            ) from exc
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
        return text


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
    return SfxPlan.model_validate_json(_json_object(text))


def _parse_summary(text: str) -> StoryBlurb:
    return StoryBlurb.model_validate_json(_json_object(text))


def _parse_picture(text: str) -> StoryCard:
    return StoryCard.model_validate_json(_json_object(text))


def _json_object(text: str) -> str:
    cleaned = _strip_fences(text)
    try:
        json.loads(cleaned)
    except ValueError:
        extracted = _extract_json_object(cleaned)
        if extracted is None:
            raise
        return extracted
    return cleaned


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
