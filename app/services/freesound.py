"""FreeSound preview downloads for story cues.

Catalog mode (the default) downloads one row from
``assets/sfx_catalog/catalog.json`` by exact catalog id. It does not search
FreeSound and it does not guess a similar sound.

Live search remains available when ``FREESOUND_CATALOG_ONLY=false``:

``GET {FREESOUND_BASE_URL}/apiv2/search/text/``
Auth: ``Authorization: Token $FREESOUND_API_KEY`` on the API host only.
Preview MP3 URLs (``previews.preview-hq-mp3``) do not need the token.

https://freesound.org/docs/api/resources_apiv2.html
https://freesound.org/docs/api/authentication.html
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from app.config import Settings
from app.services.sfx_catalog import CATALOG_PATH, load_catalog


class FreeSoundError(RuntimeError):
    """A FreeSound search or preview download failed."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class FreeSoundNotConfiguredError(FreeSoundError):
    """``FREESOUND_API_KEY`` is empty, so no request was sent."""


class FreeSoundRateLimitError(FreeSoundError):
    """FreeSound kept returning HTTP 429 after retries."""


class FreeSoundClient(Protocol):
    def download_for_query(self, query: str) -> "DownloadedClip":
        """Return the catalog preview that matches ``query``.

        When catalog-only mode is off, search FreeSound instead.
        """


@dataclass(frozen=True)
class DownloadedClip:
    sound_id: int
    name: str
    query: str
    preview_url: str
    audio_bytes: bytes
    duration_seconds: float | None = None


class HttpFreeSoundClient:
    """Download a preview MP3 for one story cue.

    In catalog-only mode ``query`` is a catalog id. That row's preview URL is
    fetched. No FreeSound search request is made, and ``FREESOUND_API_KEY``
    is not required. HTTP 429 on the preview download is retried with
    ``Retry-After`` (or a short backoff).
    """

    def __init__(
        self,
        settings: Settings,
        http: httpx.Client | None = None,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
    ) -> None:
        self._api_key = settings.freesound_api_key.strip()
        self._catalog_only = settings.freesound_catalog_only
        catalog_path = settings.sfx_catalog_path.strip()
        self._catalog_path = Path(catalog_path) if catalog_path else CATALOG_PATH
        self._catalog: dict | None = None
        self._search_url = settings.freesound_base_url.rstrip("/") + "/apiv2/search/text/"
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=settings.http_timeout_seconds)
        self._sleeper = sleeper
        self._max_retries = max(1, max_retries)
        self._backoff_seconds = backoff_seconds

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def download_for_query(self, query: str) -> DownloadedClip:
        cleaned = _clean_query(query)
        if self._catalog_only:
            return self._download_catalog_clip(cleaned)
        if not self._api_key:
            raise FreeSoundNotConfiguredError(
                "FREESOUND_API_KEY is not set. Add it to .env (see .env.example). "
                "Create a token at https://freesound.org/apiv2/apply. No request was sent."
            )
        result = self._search(cleaned)
        preview_url = _preview_url(result)
        if preview_url is None:
            sound_id = result.get("id")
            raise FreeSoundError(f"FreeSound sound {sound_id} has no MP3 preview")
        return self._clip_from_download(cleaned, result, preview_url)

    def _download_catalog_clip(self, query: str) -> DownloadedClip:
        entry = _entry_by_id(self._entries(), query)
        if entry is None:
            raise FreeSoundError(
                f"Catalog has no sound {query!r}. "
                "xAI must return a catalog id. Catalog-only mode does not search FreeSound."
            )
        preview_url = entry.get("preview_url")
        if not isinstance(preview_url, str) or not preview_url.startswith("http"):
            slot_id = entry.get("id")
            raise FreeSoundError(
                f"Catalog slot {slot_id!r} has no preview_url. "
                "Run `python scripts/build_sfx_catalog.py` with FREESOUND_API_KEY, "
                "listen to assets/sfx_catalog/previews/, and set status to approved "
                "or rejected in assets/sfx_catalog/catalog.json."
            )
        sound_id = entry.get("freesound_id") or 0
        result = {
            "id": sound_id,
            "name": entry.get("label") or query,
            "duration": entry.get("duration"),
        }
        return self._clip_from_download(query, result, preview_url)

    def _entries(self) -> list[dict]:
        if self._catalog is None:
            try:
                self._catalog = load_catalog(self._catalog_path)
            except FileNotFoundError as exc:
                raise FreeSoundNotConfiguredError(
                    f"SFX catalog not found at {self._catalog_path}. "
                    "Restore assets/sfx_catalog/catalog.json."
                ) from exc
            except (OSError, ValueError) as exc:
                raise FreeSoundError(f"SFX catalog at {self._catalog_path} could not be read: {exc}") from exc
        entries = self._catalog.get("entries")
        if not isinstance(entries, list):
            raise FreeSoundError(f"SFX catalog at {self._catalog_path} has no entries list")
        return entries

    def _clip_from_download(self, query: str, result: dict, preview_url: str) -> DownloadedClip:
        audio = self._download(preview_url)
        if not audio:
            raise FreeSoundError(f"FreeSound preview for {query!r} was empty")
        duration = result.get("duration")
        return DownloadedClip(
            sound_id=int(result.get("id") or 0),
            name=str(result.get("name") or query),
            query=query,
            preview_url=preview_url,
            audio_bytes=audio,
            duration_seconds=float(duration) if isinstance(duration, (int, float)) else None,
        )

    def _search(self, query: str) -> dict:
        response = self._request(
            "GET",
            self._search_url,
            auth=True,
            params={
                "query": query,
                "fields": "id,name,previews,duration",
                "page_size": 5,
                "sort": "rating_desc",
            },
            headers={"User-Agent": "hackgt-ted-backend/0.1"},
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise FreeSoundError("FreeSound search returned non-JSON") from exc
        results = body.get("results") or []
        for result in results:
            if isinstance(result, dict) and _preview_url(result):
                return result
        raise FreeSoundError(f"No FreeSound results for query {query!r}")

    def _download(self, preview_url: str) -> bytes:
        # Previews live on the FreeSound CDN. Do not attach the API token.
        response = self._request(
            "GET",
            preview_url,
            auth=False,
            headers={"User-Agent": "hackgt-ted-backend/0.1"},
        )
        return response.content

    def _request(self, method: str, url: str, *, auth: bool, **kwargs: object) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        if auth:
            headers["Authorization"] = f"Token {self._api_key}"
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = self._http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self._max_retries - 1:
                    self._sleeper(self._backoff_seconds * (attempt + 1))
                    continue
                raise FreeSoundError(f"FreeSound request failed: {exc}") from exc

            if response.status_code == 429 and attempt < self._max_retries - 1:
                self._sleeper(_retry_delay(response, self._backoff_seconds * (attempt + 1)))
                continue
            if response.status_code == 429:
                raise FreeSoundRateLimitError(
                    "FreeSound rate limit exceeded after retries",
                    status_code=429,
                )
            if response.status_code in {401, 403}:
                raise FreeSoundError(
                    f"FreeSound rejected FREESOUND_API_KEY (HTTP {response.status_code}).",
                    status_code=response.status_code,
                )
            if response.status_code >= 400:
                raise FreeSoundError(
                    f"FreeSound request failed (HTTP {response.status_code}).",
                    status_code=response.status_code,
                )
            return response
        raise FreeSoundError(f"FreeSound request failed: {last_error}")


def _entry_by_id(entries: list[dict], catalog_id: str) -> dict | None:
    for entry in entries:
        if str(entry.get("id") or "") != catalog_id:
            continue
        if str(entry.get("status") or "") == "rejected":
            return None
        return entry
    return None


def _clean_query(query: str) -> str:
    cleaned = " ".join(query.split())
    if not cleaned:
        raise FreeSoundError("Empty FreeSound query")
    return cleaned[:200]


def _preview_url(result: dict) -> str | None:
    previews = result.get("previews") or {}
    if not isinstance(previews, dict):
        return None
    url = previews.get("preview-hq-mp3") or previews.get("preview-lq-mp3")
    if isinstance(url, str) and url.startswith("http"):
        return url
    return None


def _retry_delay(response: httpx.Response, fallback: float) -> float:
    raw = response.headers.get("Retry-After", "").strip()
    try:
        delay = float(raw)
    except ValueError:
        delay = fallback
    return min(max(delay, 0.0), 30.0)
