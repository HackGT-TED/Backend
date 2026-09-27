"""Fixed children's-story SFX catalog.

Story requests match a cue to one entry in ``assets/sfx_catalog/catalog.json``
and download that entry's preview. They do not search FreeSound.

``scripts/build_sfx_catalog.py`` is the one-shot tool that fills each slot
with a single FreeSound sound. Ranking lives here so tests can show that the
first search hit is not kept automatically.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parents[2] / "assets" / "sfx_catalog" / "catalog.json"

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {
    "a",
    "an",
    "the",
    "of",
    "and",
    "or",
    "on",
    "in",
    "at",
    "to",
    "for",
    "with",
    "gentle",
    "quiet",
    "little",
    "small",
    "sound",
    "sounds",
    "sfx",
    "effect",
    "ambience",
    "ambient",
}
_MUSIC_TOKENS = {"music", "song", "melody", "soundtrack", "instrumental"}
_SEARCH_FIELDS = "id,name,tags,duration,license,previews,avg_rating,num_downloads,username"


def catalog_choices(entries: list[dict]) -> list[dict]:
    """Id and label only, for the xAI similarity step. Rejected slots are omitted."""

    choices: list[dict] = []
    for entry in entries:
        if str(entry.get("status") or "") == "rejected":
            continue
        slot_id = entry.get("id")
        if not slot_id:
            continue
        choices.append({"id": str(slot_id), "label": str(entry.get("label") or slot_id)})
    return choices


def load_catalog(path: Path | None = None) -> dict:
    """Read the checked-in catalog JSON."""

    catalog_path = path or CATALOG_PATH
    text = catalog_path.read_text(encoding="utf-8")
    catalog = json.loads(text)
    return ensure_catalog_document(catalog, source=f"SFX catalog at {catalog_path}")


def ensure_catalog_document(catalog: object, *, source: str = "SFX catalog") -> dict:
    """Return ``catalog`` when it is an object with an ``entries`` list."""

    if not isinstance(catalog, dict) or not isinstance(catalog.get("entries"), list):
        raise ValueError(f"{source} must be an object with an entries list")
    return catalog


def match_entry(entries: list[dict], query: str) -> dict | None:
    """Return the closest non-rejected catalog entry, or None when nothing overlaps.

    A later entry wins over an earlier one when its keyword score is higher.
    Equal scores break toward the lexicographically smaller ``id``.
    """

    best: dict | None = None
    best_score = 0
    best_id = ""
    for entry in entries:
        if str(entry.get("status") or "") == "rejected":
            continue
        score = score_query(entry, query)
        entry_id = str(entry.get("id") or "")
        if score > best_score or (score == best_score and score > 0 and entry_id < best_id):
            best = entry
            best_score = score
            best_id = entry_id
    return best


def score_query(entry: dict, query: str) -> int:
    """Keyword, label, and id overlap between a cue and one catalog slot."""

    raw = _tokens(query)
    if not raw:
        return 0
    qset = _expand(token for token in raw if token not in _STOP) or _expand(raw)
    raw_text = " ".join(raw)
    score = 0
    matched: set[str] = set()
    for keyword in entry.get("keywords") or []:
        phrase = _tokens(str(keyword))
        if not phrase:
            continue
        if len(phrase) > 1 and " ".join(phrase) in raw_text:
            score += 6
        for token in phrase:
            if token in _STOP:
                continue
            if _expand([token]) & qset:
                matched.add(_stem(token))
    score += 2 * len(matched)
    label = [token for token in _tokens(str(entry.get("label") or "")) if token not in _STOP]
    if len(label) >= 2 and set(_expand(label)) <= qset:
        score += 5
    else:
        score += 2 * len({_stem(token) for token in label if _expand([token]) & qset})
    ident = [token for token in _tokens(str(entry.get("id") or "").replace("-", " ")) if token not in _STOP]
    if len(ident) >= 2 and set(_expand(ident)) <= qset:
        score += 4
    return score


def slot_needs_search(entry: dict, *, refresh: bool) -> bool:
    """Approved slots stay put. Pending slots stay put unless ``refresh`` is set."""

    status = str(entry.get("status") or "empty")
    if status == "approved":
        return False
    if status == "pending" and entry.get("freesound_id") and not refresh:
        return False
    return True


def search_params(entry: dict) -> dict:
    """FreeSound text-search params for one slot. The caller still re-ranks hits."""

    min_duration = float(entry.get("min_duration") or 0.2)
    max_duration = float(entry.get("max_duration") or 8)
    license_filter = 'license:("Creative Commons 0" OR "Attribution")'
    return {
        "query": str(entry.get("search_query") or entry.get("label") or entry.get("id")),
        "filter": f"duration:[{min_duration:g} TO {max_duration:g}] AND {license_filter}",
        "sort": "rating_desc",
        "page_size": 20,
        "group_by_pack": 1,
        "fields": _SEARCH_FIELDS,
    }


def select_best_sound(candidates: list[dict], entry: dict, used_ids: set[int]) -> dict | None:
    """Pick one sound for ``entry``. Order in ``candidates`` is not the ranking."""

    best: dict | None = None
    best_key: tuple | None = None
    for candidate in candidates:
        scored = score_candidate(candidate, entry, used_ids)
        if scored is None:
            continue
        sound_id = int(candidate.get("id") or 0)
        downloads = int(candidate.get("num_downloads") or 0)
        rating = float(candidate.get("avg_rating") or 0)
        key = (scored, downloads, rating, -sound_id)
        if best_key is None or key > best_key:
            best = candidate
            best_key = key
    return best


def score_candidate(candidate: dict, entry: dict, used_ids: set[int]) -> float | None:
    """Rate one FreeSound hit. ``None`` means the hit cannot fill the slot.

    Music, songs, and long loops are dropped. Duration must sit inside the
    slot window. CC0 outranks plain Attribution. Rating and downloads both
    matter, so a low-download first hit loses to a stronger later hit.
    """

    try:
        sound_id = int(candidate.get("id") or 0)
    except (TypeError, ValueError):
        return None
    if sound_id <= 0 or sound_id in used_ids:
        return None
    if preview_mp3_url(candidate) is None:
        return None
    try:
        duration = float(candidate.get("duration"))
    except (TypeError, ValueError):
        return None
    min_duration = float(entry.get("min_duration") or 0.2)
    max_duration = float(entry.get("max_duration") or 8)
    if duration < min_duration or duration > max_duration:
        return None
    tags = {str(tag).lower() for tag in candidate.get("tags") or []}
    name_tokens = set(_tokens(str(candidate.get("name") or "")))
    if tags & _MUSIC_TOKENS or name_tokens & _MUSIC_TOKENS:
        return None
    if "loop" in tags and duration > 6:
        return None

    rating = float(candidate.get("avg_rating") or 0)
    downloads = int(candidate.get("num_downloads") or 0)
    if duration <= 4:
        duration_weight = 1.25
    elif duration <= 6:
        duration_weight = 1.0
    else:
        duration_weight = 0.75
    preferred = {str(tag).lower() for tag in entry.get("preferred_tags") or []}
    tag_weight = 1.0 + 0.2 * len(tags & preferred)
    query_tokens = set(_tokens(str(entry.get("search_query") or "")))
    name_weight = 1.0 + 0.1 * len(query_tokens & name_tokens)
    return (rating + 0.1) * math.log1p(max(downloads, 0)) * _license_weight(
        str(candidate.get("license") or "")
    ) * duration_weight * tag_weight * name_weight


def apply_sound(entry: dict, sound: dict, *, preview_path: str | None = None) -> None:
    """Copy one chosen FreeSound sound onto a slot and mark it pending review."""

    sound_id = int(sound["id"])
    entry["freesound_id"] = sound_id
    entry["freesound_url"] = f"https://freesound.org/s/{sound_id}/"
    entry["preview_url"] = preview_mp3_url(sound)
    entry["download_url"] = f"https://freesound.org/apiv2/sounds/{sound_id}/download/"
    entry["license"] = sound.get("license")
    entry["duration"] = sound.get("duration")
    entry["avg_rating"] = sound.get("avg_rating")
    entry["num_downloads"] = sound.get("num_downloads")
    username = sound.get("username")
    entry["username"] = str(username) if username else None
    entry["status"] = "pending"
    entry.pop("gain_db", None)
    if preview_path is not None:
        entry["preview_path"] = preview_path


def preview_mp3_url(sound: dict) -> str | None:
    """HQ preview URL, then LQ. These CDN files do not need the API token."""

    previews = sound.get("previews") or {}
    if not isinstance(previews, dict):
        direct = sound.get("preview_url")
        if isinstance(direct, str) and direct.startswith("http"):
            return direct
        return None
    url = previews.get("preview-hq-mp3") or previews.get("preview-lq-mp3")
    if isinstance(url, str) and url.startswith("http"):
        return url
    return None


def _license_weight(license_name: str) -> float:
    text = license_name.lower()
    if "creative commons 0" in text or text.strip() in {"cc0", "cc-0"}:
        return 1.45
    restricted = ("noncommercial", "non-commercial", "share alike", "sharealike", "no derivatives", "noderiv")
    if "attribution" in text and not any(marker in text for marker in restricted):
        return 1.15
    if "attribution" in text:
        return 0.65
    return 0.35


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _stem(token: str) -> str:
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _expand(tokens) -> set[str]:
    forms: set[str] = set()
    for token in tokens:
        forms.add(token)
        if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            forms.add(token[:-1])
        elif len(token) > 2:
            forms.add(token + "s")
    return forms
