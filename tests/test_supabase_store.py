"""SupabaseRecordingStore against an in-memory client with the same method chain."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.config import Settings
from app.services.store import RecordingRecord, SupabaseNotConfiguredError, SupabaseRecordingStore
import pytest


class FakeQuery:
    def __init__(self, backend: "FakeSupabase", table: str) -> None:
        self.backend = backend
        self.table = table
        self.op = "select"
        self.payload: dict | None = None
        self.filters: dict = {}
        self.order_by: tuple[str, bool] | None = None

    def insert(self, payload: dict) -> "FakeQuery":
        self.op = "insert"
        self.payload = payload
        return self

    def update(self, payload: dict) -> "FakeQuery":
        self.op = "update"
        self.payload = payload
        return self

    def select(self, *_args, **_kwargs) -> "FakeQuery":
        self.op = "select"
        return self

    def eq(self, key: str, value) -> "FakeQuery":
        self.filters[key] = value
        return self

    def order(self, column: str, *, desc: bool = False) -> "FakeQuery":
        self.order_by = (column, desc)
        return self

    def execute(self):
        rows = self.backend.tables.setdefault(self.table, [])
        if self.op == "insert":
            assert self.payload is not None
            rows.append(dict(self.payload))
            return SimpleNamespace(data=[dict(self.payload)])
        if self.op == "update":
            updated = []
            for row in rows:
                if all(row.get(key) == value for key, value in self.filters.items()):
                    row.update(self.payload or {})
                    updated.append(dict(row))
            return SimpleNamespace(data=updated)
        matched = [
            dict(row)
            for row in rows
            if all(row.get(key) == value for key, value in self.filters.items())
        ]
        if self.order_by:
            column, descending = self.order_by
            matched.sort(key=lambda row: row.get(column) or "", reverse=descending)
        return SimpleNamespace(data=matched)


class FakeSupabase:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {}
        self.uploads: dict[str, bytes] = {}
        self.bucket = ""
        self.last_options: dict | None = None

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)

    @property
    def storage(self) -> "FakeSupabase":
        return self

    def from_(self, bucket: str) -> "FakeSupabase":
        self.bucket = bucket
        return self

    def upload(self, path: str, file: bytes, file_options: dict | None = None):
        assert isinstance(file, bytes)
        self.uploads[path] = file
        self.last_options = file_options
        return SimpleNamespace(path=path)

    def get_public_url(self, path: str) -> str:
        return f"https://proj.supabase.co/storage/v1/object/public/{self.bucket}/{path}"


def _settings() -> Settings:
    return Settings(
        supabase_url="https://proj.supabase.co",
        supabase_service_role_key="service-role",
        supabase_sfx_bucket="story-sfx",
    )


def _record(recording_id: str, story_id: str | None = "story-1") -> RecordingRecord:
    return RecordingRecord(
        id=recording_id,
        story_id=story_id,
        title="Rain",
        narrator="Grandma",
        source_audio_url=None,
        transcript_text="Once upon a time the rain began to fall.",
        transcript_json={"results": {}},
        duration_seconds=8.5,
        status="processing",
    )


def test_round_trip_row_and_public_sfx_url():
    client = FakeSupabase()
    store = SupabaseRecordingStore(_settings(), client=client)
    created = store.create(_record("rec-1"))
    path, url = store.upload_sfx("rec-1", b"ID3fake-mp3")
    created.status = "ready"
    created.sfx_storage_path = path
    created.sfx_url = url
    created.cues = [{"query": "rain", "start_ms": 1280, "end_ms": 2600}]
    store.save(created)

    loaded = store.get("rec-1")
    assert loaded is not None
    assert loaded.status == "ready"
    assert loaded.transcript_text.startswith("Once upon a time")
    assert loaded.cues[0]["start_ms"] == 1280
    assert path == "rec-1/sfx.mp3"
    assert url == "https://proj.supabase.co/storage/v1/object/public/story-sfx/rec-1/sfx.mp3"
    assert client.uploads[path] == b"ID3fake-mp3"
    assert client.last_options["content-type"] == "audio/mpeg"
    assert client.bucket == "story-sfx"

    later = _record("rec-2", story_id="other")
    later.created_at = datetime(2026, 9, 26, 1, tzinfo=timezone.utc)
    later.updated_at = later.created_at
    created.created_at = later.created_at - timedelta(minutes=5)
    store.save(created)
    store.create(later)
    assert [row.id for row in store.list()] == ["rec-2", "rec-1"]
    assert [row.id for row in store.list(story_id="story-1")] == ["rec-1"]
    assert store.get("missing") is None


def test_missing_keys_do_not_build_a_client():
    store = SupabaseRecordingStore(Settings(supabase_url="", supabase_service_role_key=""))
    with pytest.raises(SupabaseNotConfiguredError, match="SUPABASE_SERVICE_ROLE_KEY"):
        store.create(_record("rec-1"))
