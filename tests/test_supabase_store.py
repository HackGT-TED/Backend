"""Supabase catalog and recording stores against an in-memory client."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services.catalog_store import (
    SupabaseCatalogStore,
    load_runtime_catalog,
    push_checked_in_catalog,
)
from app.services.sfx_catalog import CATALOG_PATH, load_catalog
from app.services.store import RecordingRecord, SupabaseNotConfiguredError, SupabaseRecordingStore


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
    raw = client.tables["recordings"][0]
    assert set(raw) == {
        "id",
        "story_id",
        "title",
        "status",
        "duration_seconds",
        "sfx_storage_path",
        "sfx_url",
        "meta",
        "created_at",
        "updated_at",
    }
    assert "transcript_json" not in raw
    assert raw["meta"]["narrator"] == "Grandma"
    assert raw["meta"]["transcript_text"].startswith("Once upon a time")
    assert raw["meta"]["cues"][0]["start_ms"] == 1280
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


def test_legacy_wide_recording_row_still_loads():
    client = FakeSupabase()
    client.tables["recordings"] = [
        {
            "id": "rec-old",
            "story_id": "s",
            "title": "Old",
            "narrator": "Aunt",
            "source_audio_url": "https://example.test/old.wav",
            "transcript_text": "hello",
            "transcript_json": {"results": {}},
            "duration_seconds": 1,
            "status": "ready",
            "error_message": None,
            "warnings": ["x"],
            "cues": [{"query": "rain"}],
            "sfx_url": "https://example/sfx.mp3",
            "created_at": "2026-09-26T00:00:00+00:00",
            "updated_at": "2026-09-26T00:00:00+00:00",
        }
    ]
    loaded = SupabaseRecordingStore(_settings(), client=client).get("rec-old")
    assert loaded is not None
    assert loaded.narrator == "Aunt"
    assert loaded.transcript_text == "hello"
    assert loaded.transcript_json == {"results": {}}
    assert loaded.cues == [{"query": "rain"}]
    assert loaded.warnings == ["x"]
    assert loaded.sfx_url == "https://example/sfx.mp3"


def test_save_and_load_active_catalog_bumps_version():
    client = FakeSupabase()
    store = SupabaseCatalogStore(_settings(), client=client)
    first = store.save_active(
        {"version": 1, "notes": "pack", "entries": [{"id": "rain", "label": "Rain", "status": "empty"}]}
    )
    assert first.id == "active"
    assert first.version == 1
    second = store.save_active(
        {"entries": [{"id": "owl-hoot", "label": "Owl hoot", "category": "animals", "status": "approved"}]}
    )
    assert second.version == 2
    loaded = store.load_active()
    assert loaded is not None
    assert loaded["entries"][0]["id"] == "owl-hoot"
    assert loaded["entries"][0]["status"] == "approved"
    raw = client.tables["sfx_catalog"][0]
    assert set(raw) == {"id", "version", "payload", "updated_at"}
    assert raw["id"] == "active"
    assert raw["version"] == 2
    assert len(client.tables["sfx_catalog"]) == 1


def test_catalog_save_rejects_a_document_without_entries():
    store = SupabaseCatalogStore(_settings(), client=FakeSupabase())
    with pytest.raises(ValueError, match="entries"):
        store.save_active({"notes": "no entries"})


def test_missing_keys_do_not_save_the_catalog():
    store = SupabaseCatalogStore(Settings(supabase_url="", supabase_service_role_key=""))
    with pytest.raises(SupabaseNotConfiguredError, match="SUPABASE_SERVICE_ROLE_KEY"):
        store.save_active({"entries": []})


def test_string_payload_is_parsed():
    client = FakeSupabase()
    client.tables["sfx_catalog"] = [
        {
            "id": "active",
            "version": 4,
            "payload": json.dumps({"entries": [{"id": "rain", "label": "Rain"}]}),
            "updated_at": "2026-09-26T00:00:00+00:00",
        }
    ]
    loaded = SupabaseCatalogStore(_settings(), client=client).load_active()
    assert loaded is not None
    assert loaded["entries"][0]["id"] == "rain"


def test_runtime_catalog_prefers_supabase_and_falls_back(tmp_path):
    client = FakeSupabase()
    settings = _settings()
    store = SupabaseCatalogStore(settings, client=client)
    store.save_active({"entries": [{"id": "owl-hoot", "label": "Owl hoot", "status": "approved"}]})
    assert load_runtime_catalog(settings, store=store)["entries"][0]["id"] == "owl-hoot"

    empty = SupabaseCatalogStore(settings, client=FakeSupabase())
    fallback = load_runtime_catalog(settings, store=empty)
    assert any(entry["id"] == "dog-bark" for entry in fallback["entries"])

    local = tmp_path / "catalog.json"
    local.write_text(json.dumps({"entries": [{"id": "local-bell"}]}), encoding="utf-8")
    pinned = Settings(
        supabase_url="https://proj.supabase.co",
        supabase_service_role_key="service-role",
        sfx_catalog_path=str(local),
    )
    assert load_runtime_catalog(pinned, store=store)["entries"][0]["id"] == "local-bell"


def test_runtime_catalog_falls_back_when_supabase_errors():
    class Offline:
        def table(self, _name: str):
            raise RuntimeError("offline")

    settings = _settings()
    store = SupabaseCatalogStore(settings, client=Offline())
    loaded = load_runtime_catalog(settings, store=store)
    assert any(entry["id"] == "rain" for entry in loaded["entries"])


def test_unconfigured_supabase_uses_the_checked_in_catalog():
    loaded = load_runtime_catalog(Settings(supabase_url="", supabase_service_role_key=""))
    assert loaded["entries"] == load_catalog(CATALOG_PATH)["entries"]


def test_checked_in_catalog_round_trips_through_the_store():
    client = FakeSupabase()
    settings = _settings()
    record = push_checked_in_catalog(settings, client=client)
    assert record.version == 1
    assert record.payload["entries"][0]["id"]
    loaded = load_runtime_catalog(settings, store=SupabaseCatalogStore(settings, client=client))
    assert [entry["id"] for entry in loaded["entries"]] == [
        entry["id"] for entry in record.payload["entries"]
    ]
    assert loaded["entries"][0]["keywords"]
