"""Fill the fixed kids-book SFX catalog with one FreeSound sound per slot.

Story requests never call this. Run it once (and again after you reject a slot):

    python scripts/build_sfx_catalog.py
    python scripts/build_sfx_catalog.py --refresh
    python scripts/build_sfx_catalog.py --no-download
    python scripts/build_sfx_catalog.py --push

``FREESOUND_API_KEY`` is required only when a slot still needs a search. The
token is sent only to the FreeSound API host. Measuring previews and ``--push``
do not need it. Preview files are written under ``assets/sfx_catalog/previews/``
and are gitignored.

Approved entries are left unchanged. Pending entries that already have a
FreeSound id are left unchanged unless ``--refresh`` is set. Empty and
rejected slots get a new candidate, marked ``pending`` so you can listen
before trusting them.

Every non-rejected slot with a preview is measured, and ``gain_db`` is written
onto that entry so the clips share one integrated loudness. The preview MP3
written next to the catalog is that leveled file. ``sfx_level_db`` on the
document is the extra offset applied only when a story is mixed. ``--push``
stores that same JSON as the active Supabase catalog.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.loudness import (  # noqa: E402
    DEFAULT_SFX_LEVEL_DB,
    TARGET_LUFS,
    leveled_preview_bytes,
    record_clip_gain,
)
from app.services.sfx_catalog import (  # noqa: E402
    CATALOG_PATH,
    apply_sound,
    search_params,
    select_best_sound,
    slot_needs_search,
)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Choose one FreeSound preview per catalog slot.")
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH, help="Catalog JSON to update.")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-pick pending slots. Approved slots are still kept.",
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="Update catalog.json without writing preview MP3s.",
    )
    parser.add_argument("--sleep", type=float, default=0.4, help="Seconds to wait between searches.")
    parser.add_argument(
        "--push",
        action="store_true",
        help="After writing the file, store it as the active sfx_catalog row in Supabase.",
    )
    args = parser.parse_args(argv)

    _load_dotenv(ROOT / ".env")
    api_key = os.environ.get("FREESOUND_API_KEY", "").strip()

    catalog_path = args.catalog
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    entries = catalog.get("entries")
    if not isinstance(entries, list):
        print(f"{catalog_path} has no entries list", file=sys.stderr)
        return 1

    needs_search = any(
        isinstance(entry, dict) and slot_needs_search(entry, refresh=args.refresh) for entry in entries
    )
    if needs_search and not api_key:
        print(
            "FREESOUND_API_KEY is not set, so empty slots will not be searched. "
            "Existing previews will still be measured. "
            "Add the key to .env to fill empty slots (https://freesound.org/apiv2/apply).",
            file=sys.stderr,
        )

    catalog["loudness_target_lufs"] = TARGET_LUFS
    catalog.setdefault("sfx_level_db", DEFAULT_SFX_LEVEL_DB)
    catalog = _with_loudness_target(catalog)
    base_url = os.environ.get("FREESOUND_BASE_URL", "https://freesound.org").rstrip("/")
    search_url = base_url + "/apiv2/search/text/"
    previews_dir = catalog_path.parent / "previews"
    used_ids = _reserved_ids(entries, refresh=args.refresh)
    failures = 0
    download = not args.no_download

    with httpx.Client(timeout=30.0) as http:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            slot_id = str(entry.get("id") or "sound")
            picked = False
            if slot_needs_search(entry, refresh=args.refresh):
                if not api_key:
                    print(f"{slot_id}: skipped search (no FREESOUND_API_KEY)")
                    continue
                try:
                    sound = _search_best(http, search_url, api_key, entry, used_ids)
                except httpx.HTTPError as exc:
                    print(f"{slot_id}: search failed ({exc})", file=sys.stderr)
                    failures += 1
                    time.sleep(max(args.sleep, 0.0))
                    continue
                if sound is None:
                    print(f"{slot_id}: no suitable preview in the filtered results")
                    failures += 1
                else:
                    apply_sound(entry, sound, preview_path=_preview_rel(previews_dir, slot_id))
                    used_ids.add(int(sound["id"]))
                    picked = True
                    print(
                        f"{slot_id}: picked {sound.get('id')} "
                        f"{sound.get('name')!r} ({sound.get('license')}, {sound.get('duration')}s)"
                    )
                time.sleep(max(args.sleep, 0.0))
            if str(entry.get("status") or "") == "rejected" or not entry.get("preview_url"):
                continue
            try:
                audio = _preview_bytes(http, entry, previews_dir, save=download, force=picked)
            except httpx.HTTPError as exc:
                print(f"{slot_id}: preview fetch failed ({exc})", file=sys.stderr)
                failures += 1
                continue
            if not audio:
                continue
            gain = record_clip_gain(entry, audio)
            if gain is None:
                print(f"{slot_id}: preview could not be measured", file=sys.stderr)
                failures += 1
                continue
            print(f"{slot_id}: gain_db {gain:+.1f}")
            if download:
                _write_leveled_preview(entry, previews_dir, audio, gain)

    catalog_path.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {catalog_path}")
    if args.push and _push_catalog(catalog):
        failures += 1
    return 1 if failures else 0


def _with_loudness_target(catalog: dict) -> dict:
    """Keep the loudness fields with the other document metadata."""

    target = catalog.get("loudness_target_lufs", catalog.get("loudness_target_dbfs", TARGET_LUFS))
    level = catalog.get("sfx_level_db", DEFAULT_SFX_LEVEL_DB)
    ordered: dict = {}
    for key, value in catalog.items():
        if key in {"loudness_target_lufs", "loudness_target_dbfs", "sfx_level_db"}:
            continue
        ordered[key] = value
        if key == "notes":
            ordered["loudness_target_lufs"] = target
            ordered["sfx_level_db"] = level
    if "loudness_target_lufs" not in ordered:
        ordered["loudness_target_lufs"] = target
        ordered["sfx_level_db"] = level
    return ordered


def _push_catalog(catalog: dict) -> bool:
    """Store the catalog JSON. Return True when that save failed."""

    from app.config import Settings
    from app.services.catalog_store import save_active_catalog
    from app.services.store import SupabaseError

    try:
        record = save_active_catalog(catalog, Settings())
    except (SupabaseError, ValueError, OSError) as exc:
        print(
            f"catalog file was written, but Supabase save failed: {exc}",
            file=sys.stderr,
        )
        return True
    print(f"stored sfx_catalog id={record.id} version={record.version}")
    return False


def _reserved_ids(entries: list[dict], *, refresh: bool) -> set[int]:
    reserved: set[int] = set()
    for entry in entries:
        if slot_needs_search(entry, refresh=refresh):
            continue
        sound_id = entry.get("freesound_id")
        if sound_id:
            reserved.add(int(sound_id))
    return reserved


def _search_best(
    http: httpx.Client,
    search_url: str,
    api_key: str,
    entry: dict,
    used_ids: set[int],
) -> dict | None:
    response = _request(
        http,
        "GET",
        search_url,
        headers={
            "Authorization": f"Token {api_key}",
            "User-Agent": "hackgt-ted-backend/catalog",
        },
        params=search_params(entry),
    )
    body = response.json()
    results = body.get("results") or []
    sounds = [item for item in results if isinstance(item, dict)]
    return select_best_sound(sounds, entry, used_ids)


def _preview_bytes(
    http: httpx.Client,
    entry: dict,
    previews_dir: Path,
    *,
    save: bool,
    force: bool,
) -> bytes | None:
    """Return the original preview bytes.

    The raw file is cached under ``previews/source/``. The file in ``previews/``
    is written later with the match gain applied, so a second run must not
    measure that copy.
    """

    slot_id = str(entry.get("id") or "sound")
    source = previews_dir / "source" / f"{slot_id}.mp3"
    url = entry.get("preview_url")
    if not isinstance(url, str) or not url.startswith("http"):
        return None
    if source.is_file() and source.stat().st_size > 0 and not force:
        print(f"{slot_id}: source preview already on disk")
        return source.read_bytes()
    # CDN previews are public. Do not send the API token.
    response = _request(http, "GET", url, headers={"User-Agent": "hackgt-ted-backend/catalog"})
    if save:
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(response.content)
    return response.content


def _write_leveled_preview(entry: dict, previews_dir: Path, raw: bytes, gain_db: float) -> None:
    slot_id = str(entry.get("id") or "sound")
    destination = previews_dir / f"{slot_id}.mp3"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(leveled_preview_bytes(raw, gain_db))
    entry["preview_path"] = _preview_rel(previews_dir, slot_id)
    print(f"{slot_id}: wrote {entry['preview_path']}")


def _preview_rel(previews_dir: Path, slot_id: str) -> str:
    destination = previews_dir / f"{slot_id}.mp3"
    try:
        return destination.relative_to(ROOT).as_posix()
    except ValueError:
        return destination.as_posix()


def _request(http: httpx.Client, method: str, url: str, **kwargs: object) -> httpx.Response:
    delay = 0.5
    last: httpx.Response | None = None
    for attempt in range(3):
        response = http.request(method, url, **kwargs)
        last = response
        if response.status_code == 429 and attempt < 2:
            retry_after = response.headers.get("Retry-After", "")
            try:
                time.sleep(min(float(retry_after), 30.0))
            except ValueError:
                time.sleep(delay)
                delay *= 2
            continue
        if response.status_code in {401, 403}:
            raise SystemExit(
                f"FreeSound rejected FREESOUND_API_KEY (HTTP {response.status_code}). "
                "No catalog changes were written yet."
            )
        response.raise_for_status()
        return response
    assert last is not None
    last.raise_for_status()
    return last


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    raise SystemExit(main())
