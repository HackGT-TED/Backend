"""Supabase persistence for mixed story MP3s.

The catalog document lives in ``app.services.catalog_store``. This module only
keeps the minimal recording row (status plus public MP3 URL) and uploads the
mix into the ``story-sfx`` bucket. Transcript text, cues, and warnings sit in
the ``meta`` jsonb column so the story API can still return them.

The server uses the service role key. The anon key is not read. Marketplace
and social data are not stored here.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from app.config import Settings

TABLE = "recordings"


class RecordingStore(Protocol):
    def create(self, record: "RecordingRecord") -> "RecordingRecord":
        """Insert a new recording row."""

    def save(self, record: "RecordingRecord") -> "RecordingRecord":
        """Update an existing recording row."""

    def get(self, recording_id: str) -> "RecordingRecord | None":
        """Return one recording, or None when it does not exist."""

    def list(self, story_id: str | None = None) -> list["RecordingRecord"]:
        """Return recordings, newest first. Filter by ``story_id`` when set."""

    def upload_sfx(self, recording_id: str, audio_bytes: bytes) -> tuple[str, str]:
        """Store MP3 bytes and return ``(storage_path, public_url)``."""

    def upload_cover(self, image_bytes: bytes, content_type: str = "image/jpeg") -> str:
        """Store a cover image and return its public URL."""


class SupabaseError(RuntimeError):
    """A Supabase table or storage call failed."""


class SupabaseNotConfiguredError(SupabaseError):
    """``SUPABASE_URL`` or ``SUPABASE_SERVICE_ROLE_KEY`` is empty."""


@dataclass
class RecordingRecord:
    id: str
    story_id: str | None
    title: str | None
    narrator: str | None
    source_audio_url: str | None
    transcript_text: str
    transcript_json: dict[str, Any]
    duration_seconds: float
    status: str
    error_message: str | None = None
    warnings: list[str] = field(default_factory=list)
    sfx_storage_path: str | None = None
    sfx_url: str | None = None
    cues: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SupabaseRecordingStore:
    """PostgREST plus Storage via the official supabase-py client."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client
        self._bucket = settings.supabase_sfx_bucket

    def create(self, record: RecordingRecord) -> RecordingRecord:
        execute_query(self._supabase().table(TABLE).insert(_to_row(record)))
        return record

    def save(self, record: RecordingRecord) -> RecordingRecord:
        record.updated_at = datetime.now(timezone.utc)
        response = execute_query(
            self._supabase().table(TABLE).update(_to_row(record)).eq("id", record.id)
        )
        if not rows_of(response):
            raise SupabaseError(f"Recording {record.id} was not updated")
        return record

    def get(self, recording_id: str) -> RecordingRecord | None:
        response = execute_query(
            self._supabase().table(TABLE).select("*").eq("id", recording_id)
        )
        found = rows_of(response)
        if not found:
            return None
        return _from_row(found[0])

    def list(self, story_id: str | None = None) -> list[RecordingRecord]:
        query = self._supabase().table(TABLE).select("*")
        if story_id is not None:
            query = query.eq("story_id", story_id)
        response = execute_query(query.order("created_at", desc=True))
        return [_from_row(row) for row in rows_of(response)]

    def upload_sfx(self, recording_id: str, audio_bytes: bytes) -> tuple[str, str]:
        if not audio_bytes:
            raise SupabaseError("Refusing to upload an empty SFX file")
        path = f"{recording_id}/sfx.mp3"
        try:
            self._supabase().storage.from_(self._bucket).upload(
                path,
                audio_bytes,
                file_options={"content-type": "audio/mpeg", "upsert": "true"},
            )
            url = self._supabase().storage.from_(self._bucket).get_public_url(path)
        except SupabaseNotConfiguredError:
            raise
        except SupabaseError:
            raise
        except Exception as exc:
            raise SupabaseError(f"Supabase storage upload failed: {exc}") from exc
        if not isinstance(url, str) or not url:
            raise SupabaseError("Supabase did not return a public URL for the SFX object")
        return path, url

    def upload_cover(self, image_bytes: bytes, content_type: str = "image/jpeg") -> str:
        if not image_bytes:
            raise SupabaseError("Refusing to upload an empty cover image")
        extension = _image_extension(content_type)
        path = f"covers/{uuid.uuid4()}.{extension}"
        try:
            self._supabase().storage.from_(self._bucket).upload(
                path,
                image_bytes,
                file_options={"content-type": content_type, "upsert": "true"},
            )
            url = self._supabase().storage.from_(self._bucket).get_public_url(path)
        except SupabaseNotConfiguredError:
            raise
        except SupabaseError:
            raise
        except Exception as exc:
            raise SupabaseError(f"Supabase storage upload failed: {exc}") from exc
        if not isinstance(url, str) or not url:
            raise SupabaseError("Supabase did not return a public URL for the cover image")
        return url

    def _supabase(self) -> Any:
        if self._client is None:
            self._client = open_supabase(self._settings)
        return self._client


def open_supabase(settings: Settings) -> Any:
    """Build a service-role client. The anon key is not read."""

    url = settings.supabase_url.strip()
    key = settings.supabase_service_role_key.strip()
    if not url or not key:
        raise SupabaseNotConfiguredError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set. "
            "The API uses the service role key on the server only. "
            "Do not ship that key to the web or kids apps. "
            "SUPABASE_ANON_KEY is not used."
        )
    try:
        from supabase import create_client
    except ImportError as exc:
        raise SupabaseError("The supabase package is not installed") from exc
    try:
        return create_client(url, key)
    except Exception as exc:
        raise SupabaseError(f"Could not create the Supabase client: {exc}") from exc


def execute_query(query: Any) -> Any:
    """Run a PostgREST query and wrap client failures as ``SupabaseError``."""

    try:
        return query.execute()
    except SupabaseNotConfiguredError:
        raise
    except SupabaseError:
        raise
    except Exception as exc:
        raise SupabaseError(f"Supabase request failed: {exc}") from exc


def rows_of(response: Any) -> list[dict[str, Any]]:
    data = getattr(response, "data", None)
    if data is None and isinstance(response, dict):
        data = response.get("data")
    if not data:
        return []
    return list(data)


def _image_extension(content_type: str) -> str:
    kind = content_type.split(";", 1)[0].strip().lower()
    if kind == "image/png":
        return "png"
    if kind == "image/webp":
        return "webp"
    return "jpg"


def to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _to_row(record: RecordingRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "story_id": record.story_id,
        "title": record.title,
        "status": record.status,
        "duration_seconds": record.duration_seconds,
        "sfx_storage_path": record.sfx_storage_path,
        "sfx_url": record.sfx_url,
        "meta": {
            "narrator": record.narrator,
            "source_audio_url": record.source_audio_url,
            "transcript_text": record.transcript_text,
            "transcript_json": record.transcript_json,
            "error_message": record.error_message,
            "warnings": list(record.warnings),
            "cues": list(record.cues),
        },
        "created_at": to_iso(record.created_at),
        "updated_at": to_iso(record.updated_at),
    }


def _from_row(row: dict[str, Any]) -> RecordingRecord:
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}

    def pick(key: str, default: Any = None) -> Any:
        if key in meta:
            return meta[key]
        if key in row:
            return row[key]
        return default

    return RecordingRecord(
        id=str(row["id"]),
        story_id=row.get("story_id"),
        title=row.get("title"),
        narrator=pick("narrator"),
        source_audio_url=pick("source_audio_url"),
        transcript_text=pick("transcript_text") or "",
        transcript_json=pick("transcript_json") or {},
        duration_seconds=float(row.get("duration_seconds") or 0),
        status=row.get("status") or "processing",
        error_message=pick("error_message"),
        warnings=list(pick("warnings") or []),
        sfx_storage_path=row.get("sfx_storage_path"),
        sfx_url=row.get("sfx_url"),
        cues=list(pick("cues") or []),
        created_at=parse_time(row.get("created_at")),
        updated_at=parse_time(row.get("updated_at")),
    )


def parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        text = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)
