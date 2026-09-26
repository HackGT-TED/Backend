"""Fill empty picture-book and children's-adventure SFX slots from FreeSound.

The checked-in catalog (``assets/sfx_catalog/catalog.json``) is 100+ slots,
including cartoon-safe action. This script does not invent FreeSound ids: a
slot stays empty when the search returns nothing usable.

Story requests never call this. Run it once (and again after you reject a slot):

    python scripts/build_sfx_catalog.py
    python scripts/build_sfx_catalog.py --refresh
    python scripts/build_sfx_catalog.py --no-download

Requires ``FREESOUND_API_KEY`` in the environment or in ``.env``. The token is
sent only to the FreeSound API host. Preview files are written under
``assets/sfx_catalog/previews/`` and are gitignored.

Approved entries are left unchanged. Pending entries that already have a
FreeSound id are left unchanged unless ``--refresh`` is set. Empty and
rejected slots get a new candidate, marked ``pending`` so you can listen
before trusting them.
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
    args = parser.parse_args(argv)

    _load_dotenv(ROOT / ".env")
    api_key = os.environ.get("FREESOUND_API_KEY", "").strip()
    if not api_key:
        print(
            "FREESOUND_API_KEY is not set. Add it to .env (see .env.example). "
            "Create a token at https://freesound.org/apiv2/apply",
            file=sys.stderr,
        )
        return 1

    catalog_path = args.catalog
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    entries = catalog.get("entries")
    if not isinstance(entries, list):
        print(f"{catalog_path} has no entries list", file=sys.stderr)
        return 1

    base_url = os.environ.get("FREESOUND_BASE_URL", "https://freesound.org").rstrip("/")
    search_url = base_url + "/apiv2/search/text/"
    previews_dir = catalog_path.parent / "previews"
    used_ids = _reserved_ids(entries, refresh=args.refresh)
    failures = 0
    download = not args.no_download

    with httpx.Client(timeout=30.0) as http:
        for entry in entries:
            slot_id = str(entry.get("id") or "sound")
            if slot_needs_search(entry, refresh=args.refresh):
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
                    print(
                        f"{slot_id}: picked {sound.get('id')} "
                        f"{sound.get('name')!r} ({sound.get('license')}, {sound.get('duration')}s)"
                    )
                time.sleep(max(args.sleep, 0.0))
            if download and entry.get("preview_url") and entry.get("status") != "rejected":
                _download_preview(http, entry, previews_dir)

    catalog_path.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {catalog_path}")
    return 1 if failures else 0


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


def _download_preview(http: httpx.Client, entry: dict, previews_dir: Path) -> None:
    slot_id = str(entry.get("id") or "sound")
    destination = previews_dir / f"{slot_id}.mp3"
    rel = _preview_rel(previews_dir, slot_id)
    if destination.is_file() and destination.stat().st_size > 0:
        entry["preview_path"] = rel
        print(f"{slot_id}: preview already on disk")
        return
    url = entry.get("preview_url")
    if not isinstance(url, str) or not url.startswith("http"):
        return
    # CDN previews are public. Do not send the API token.
    response = _request(http, "GET", url, headers={"User-Agent": "hackgt-ted-backend/catalog"})
    previews_dir.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(response.content)
    entry["preview_path"] = rel
    print(f"{slot_id}: wrote {rel}")


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
