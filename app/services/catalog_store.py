"""Load and save the SFX catalog document in Supabase.

The active row (``id = 'active'``) is the allowed sound library the pipeline
uses. ``assets/sfx_catalog/catalog.json`` is the fallback for local and dev
when Supabase is unset, empty, or unreachable.

Marketplace and social data are not stored here.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.services.sfx_catalog import CATALOG_PATH, ensure_catalog_document, load_catalog
from app.services.store import (
    SupabaseError,
    SupabaseNotConfiguredError,
    execute_query,
    open_supabase,
    rows_of,
    to_iso,
)

logger = logging.getLogger(__name__)

CATALOG_TABLE = "sfx_catalog"
ACTIVE_CATALOG_ID = "active"


@dataclass
class CatalogRecord:
    """One stored catalog revision."""

    id: str
    version: int
    payload: dict[str, Any]
    updated_at: datetime


class SupabaseCatalogStore:
    """Read and upsert the active catalog JSON via the service role key."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client

    def load_active(self) -> dict[str, Any] | None:
        """Return the active catalog document, or None when the row is missing."""

        response = execute_query(
            self._supabase()
            .table(CATALOG_TABLE)
            .select("id,version,payload,updated_at")
            .eq("id", ACTIVE_CATALOG_ID)
        )
        found = rows_of(response)
        if not found:
            return None
        return payload_from_row(found[0])

    def save_active(self, catalog: dict[str, Any]) -> CatalogRecord:
        """Insert or replace the active catalog and bump ``version``."""

        payload = copy.deepcopy(ensure_catalog_document(catalog, source="SFX catalog"))
        existing = self._row(ACTIVE_CATALOG_ID)
        now = datetime.now(timezone.utc)
        version = 1 if existing is None else int(existing.get("version") or 0) + 1
        row = {
            "id": ACTIVE_CATALOG_ID,
            "version": version,
            "payload": payload,
            "updated_at": to_iso(now),
        }
        if existing is None:
            execute_query(self._supabase().table(CATALOG_TABLE).insert(row))
        else:
            response = execute_query(
                self._supabase().table(CATALOG_TABLE).update(row).eq("id", ACTIVE_CATALOG_ID)
            )
            if not rows_of(response):
                raise SupabaseError("Active SFX catalog was not updated")
        return CatalogRecord(
            id=ACTIVE_CATALOG_ID,
            version=version,
            payload=payload,
            updated_at=now,
        )

    def _row(self, catalog_id: str) -> dict[str, Any] | None:
        response = execute_query(
            self._supabase().table(CATALOG_TABLE).select("id,version").eq("id", catalog_id)
        )
        found = rows_of(response)
        if not found:
            return None
        return found[0]

    def can_query(self) -> bool:
        """True when a client is already injected or both Supabase keys are set."""

        if self._client is not None:
            return True
        return bool(
            self._settings.supabase_url.strip() and self._settings.supabase_service_role_key.strip()
        )

    def _supabase(self) -> Any:
        if self._client is None:
            self._client = open_supabase(self._settings)
        return self._client


def load_runtime_catalog(
    settings: Settings,
    store: SupabaseCatalogStore | None = None,
) -> dict[str, Any]:
    """Active Supabase catalog, or the checked-in JSON when that is unavailable.

    ``SFX_CATALOG_PATH`` always wins so local runs can pin a file. With that
    unset, a configured project is read first. A missing row, a bad payload,
    or a failed request falls back to ``assets/sfx_catalog/catalog.json``.
    """

    override = settings.sfx_catalog_path.strip()
    if override:
        return load_catalog(Path(override))

    catalog_store = store or SupabaseCatalogStore(settings)
    if not catalog_store.can_query():
        return load_catalog(CATALOG_PATH)
    try:
        payload = catalog_store.load_active()
    except SupabaseNotConfiguredError:
        return load_catalog(CATALOG_PATH)
    except Exception as exc:
        logger.warning(
            "SFX catalog in Supabase is unavailable (%s); using %s",
            exc,
            CATALOG_PATH,
        )
        return load_catalog(CATALOG_PATH)
    if payload is None:
        logger.info("No active SFX catalog in Supabase; using %s", CATALOG_PATH)
        return load_catalog(CATALOG_PATH)
    return payload


def save_active_catalog(
    catalog: dict[str, Any],
    settings: Settings,
    client: Any | None = None,
) -> CatalogRecord:
    """Store ``catalog`` as the active ``sfx_catalog`` row."""

    return SupabaseCatalogStore(settings, client=client).save_active(catalog)


def push_checked_in_catalog(
    settings: Settings,
    *,
    path: Path | None = None,
    client: Any | None = None,
) -> CatalogRecord:
    """Save the on-disk catalog JSON as the active Supabase row."""

    catalog_path = path or catalog_file(settings)
    return save_active_catalog(load_catalog(catalog_path), settings, client=client)


def catalog_file(settings: Settings) -> Path:
    """Path override, or the checked-in catalog."""

    override = settings.sfx_catalog_path.strip()
    return Path(override) if override else CATALOG_PATH


def payload_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Validate the ``payload`` column of one ``sfx_catalog`` row."""

    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("SFX catalog payload is not valid JSON") from exc
    return ensure_catalog_document(payload, source="SFX catalog payload")
