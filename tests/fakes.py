"""In-memory stand-ins for Supabase, used by API tests."""

from app.services.store import RecordingRecord


class MemoryRecordingStore:
    def __init__(self) -> None:
        self.rows: dict[str, RecordingRecord] = {}
        self.files: dict[str, bytes] = {}

    def create(self, record: RecordingRecord) -> RecordingRecord:
        self.rows[record.id] = record
        return record

    def save(self, record: RecordingRecord) -> RecordingRecord:
        self.rows[record.id] = record
        return record

    def get(self, recording_id: str) -> RecordingRecord | None:
        return self.rows.get(recording_id)

    def list(self, story_id: str | None = None) -> list[RecordingRecord]:
        rows = list(self.rows.values())
        if story_id is not None:
            rows = [row for row in rows if row.story_id == story_id]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)

    def upload_sfx(self, recording_id: str, audio_bytes: bytes) -> tuple[str, str]:
        path = f"{recording_id}/sfx.mp3"
        self.files[path] = audio_bytes
        url = f"https://example.supabase.co/storage/v1/object/public/story-sfx/{path}"
        return path, url
