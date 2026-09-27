"""Grok Imagine cover for one story.

``POST {XAI_BASE_URL}/images/generations`` with ``grok-imagine-image-2.0``.
The chat model only supplies the story moment. This module owns the style
instructions so every cover stays a playful picture book.
"""

import httpx

from app.config import Settings
from app.schemas.api import StoryCard
from app.services.xai import XaiAuthError, XaiError, XaiNotConfiguredError, xai_timeout

_STYLE = """Picture-book cover for ages 4 to 8. One playful moment, not a poster and not a collage.

Painted like a favorite children's book: gouache and colored pencil on warm paper, visible brush marks, a little paper grain, slightly wobbly outlines, chunky friendly shapes, bright colors that are not neon. A wink of humor. Looks made by a person with a pencil, not a shiny 3D render.

Child-friendly: everyone is dressed, nobody is hurt, no weapons, no blood, no monsters jumping out, no horror, no romantic pose. Faces are simple and kind. Hands look like storybook hands. No photoreal skin, no plastic glow, no uncanny eyes, no extra limbs, no tiny nonsense letters, no watermark, no logo, no frame, no caption.
"""


def cover_prompt(card: StoryCard, duration_seconds: float) -> str:
    """Story-specific scene plus the fixed picture-book style."""

    feeling = ", ".join(card.hashtags) if card.hashtags else "gentle"
    if duration_seconds >= 45 or "bedtime" in card.hashtags:
        pace = "A cozy, unhurried moment."
    elif duration_seconds <= 20:
        pace = "One small, punchy moment."
    else:
        pace = "One clear story moment."
    return (
        f"{_STYLE}\n"
        f"Story: {card.description}\n"
        f"Picture this: {card.scene}\n"
        f"Feeling: {feeling}. {pace}"
    )


class HttpImagineClient:
    """One image per story. A missing key fails before any HTTP call."""

    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self._api_key = settings.xai_api_key.strip()
        self._model = settings.xai_image_model
        self._url = settings.xai_base_url.rstrip("/") + "/images/generations"
        self._timeout = xai_timeout(settings)
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=self._timeout)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def generate(self, prompt: str) -> str:
        """Return a temporary image URL. Callers should save it promptly."""

        if not self._api_key:
            raise XaiNotConfiguredError(
                "XAI_API_KEY is not set. Add it to .env (see .env.example). "
                "No request was sent."
            )
        if not prompt.strip():
            raise XaiError("Image prompt was empty, so no image was requested.")
        try:
            response = self._http.post(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "prompt": prompt,
                    "n": 1,
                    "aspect_ratio": "1:1",
                    "resolution": "1k",
                    "quality": "low",
                    "response_format": "url",
                },
            )
        except httpx.TimeoutException as exc:
            raise XaiError(
                f"Grok Imagine did not answer within {self._timeout.read:.0f}s."
            ) from exc
        except httpx.HTTPError as exc:
            raise XaiError(f"Grok Imagine request failed: {exc}") from exc

        if response.status_code in {401, 403}:
            raise XaiAuthError(
                f"xAI rejected XAI_API_KEY (HTTP {response.status_code})."
            )
        if response.status_code >= 400:
            detail = response.text[:500]
            raise XaiError(f"Grok Imagine request failed (HTTP {response.status_code}): {detail}")
        return _image_url(response.json())


def _image_url(body: object) -> str:
    if not isinstance(body, dict):
        raise XaiError("Unexpected Grok Imagine response shape")
    data = body.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise XaiError("Grok Imagine returned no image")
    url = data[0].get("url")
    if not isinstance(url, str) or not url.startswith("http"):
        raise XaiError("Grok Imagine returned no image URL")
    return url
