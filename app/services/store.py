"""Supabase persistence for recordings and SFX MP3s.

The server uses the service role key. The anon key is not read. Row access goes
through this API; the ``story-sfx`` bucket is public so clients can play the URL
this module stores on the recording.
"""

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
        self._execute(self._supabase().table(TABLE).insert(_to_row(record)))
        return record

    def save(self, record: RecordingRecord) -> RecordingRecord:
        record.updated_at = datetime.now(timezone.utc)
        response = self._execute(
            self._supabase().table(TABLE).update(_to_row(record)).eq("id", record.id)
        )
        if not _rows(response):
            raise SupabaseError(f"Recording {record.id} was not updated")
        return record

    def get(self, recording_id: str) -> RecordingRecord | None:
        response = self._execute(
            self._supabase().table(TABLE).select("*").eq("id", recording_id)
        )
        rows = _rows(response)
        if not rows:
            return None
        return _from_row(rows[0])

    def list(self, story_id: str | None = None) -> list[RecordingRecord]:
        query = self._supabase().table(TABLE).select("*")
        if story_id is not None:
            query = query.eq("story_id", story_id)
        response = self._execute(query.order("created_at", desc=True))
        return [_from_row(row) for row in _rows(response)]

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

    def _supabase(self) -> Any:
        if self._client is not None:
            return self._client
        url = self._settings.supabase_url.strip()
        key = self._settings.supabase_service_role_key.strip()
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
            self._client = create_client(url, key)
        except Exception as exc:
            raise SupabaseError(f"Could not create the Supabase client: {exc}") from exc
        return self._client

    def _execute(self, query: Any) -> Any:
        try:
            return query.execute()
        except SupabaseNotConfiguredError:
            raise
        except SupabaseError:
            raise
        except Exception as exc:
            raise SupabaseError(f"Supabase request failed: {exc}") from exc


def _to_row(record: RecordingRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "story_id": record.story_id,
        "title": record.title,
        "narrator": record.narrator,
        "source_audio_url": record.source_audio_url,
        "transcript_text": record.transcript_text,
        "transcript_json": record.transcript_json,
        "duration_seconds": record.duration_seconds,
        "status": record.status,
        "error_message": record.error_message,
        "warnings": list(record.warnings),
        "sfx_storage_path": record.sfx_storage_path,
        "sfx_url": record.sfx_url,
        "cues": list(record.cues),
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
    }


def _from_row(row: dict[str, Any]) -> RecordingRecord:
    return RecordingRecord(
        id=str(row["id"]),
        story_id=row.get("story_id"),
        title=row.get("title"),
        narrator=row.get("narrator"),
        source_audio_url=row.get("source_audio_url"),
        transcript_text=row.get("transcript_text") or "",
        transcript_json=row.get("transcript_json") or {},
        duration_seconds=float(row.get("duration_seconds") or 0),
        status=row.get("status") or "processing",
        error_message=row.get("error_message"),
        warnings=list(row.get("warnings") or []),
        sfx_storage_path=row.get("sfx_storage_path"),
        sfx_url=row.get("sfx_url"),
        cues=list(row.get("cues") or []),
        created_at=_parse_time(row.get("created_at")),
        updated_at=_parse_time(row.get("updated_at")),
    )


def _rows(response: Any) -> list[dict[str, Any]]:
    data = getattr(response, "data", None)
    if data is None and isinstance(response, dict):
        data = response.get("data")
    if not data:
        return []
    return list(data)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        text = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)
