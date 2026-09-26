#!/usr/bin/env python3
"""Local smoke test: xAI picks catalog ids, then local clips are mixed.

This does not call FreeSound and it does not start the API. It sends a short
prompt (transcript, word times, utterances, catalog ids) to xAI and overlays
the matching files from disk onto a silent bed.

    pip install -e ".[dev]"
    pip install -r scripts/local_sfx_test/requirements.txt
    python scripts/local_sfx_test/run_local_sfx_test.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.mixer import TimedClip, mix_sfx_mp3  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_STORY = HERE / "deepgram_story.json"
DEFAULT_CATALOG = ROOT / "assets" / "sfx_catalog" / "catalog.json"
DEFAULT_CLIPS = HERE / "catalog_clips"
PREVIEW_CLIPS = ROOT / "assets" / "sfx_catalog" / "previews"
DEFAULT_OUT = HERE / "out"
CLIP_SUFFIXES = (".mp3", ".wav", ".ogg", ".flac", ".m4a")

FALLBACK_CATALOG_IDS = [
    "rain",
    "door-creak",
    "bird-chirp",
    "dog-bark",
    "footsteps-wood",
    "wind",
    "thunder",
    "pages-turning",
    "magic-chime",
    "kettle-whistle",
    "owl-hoot",
    "yawn",
]

SYSTEM_PROMPT = """You place sound effects on a children's bedtime story.
Use only catalog_id values copied exactly from available_catalog.
Return at most 12 cues. start and end are seconds on the story clock.
start must be >= 0 and end must be <= duration_seconds. end must be greater than start.
Align each cue to the words it illustrates. reason is one short sentence.
Do not invent catalog ids. If nothing fits, return an empty cues array.
"""

RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "catalog_sfx_plan",
        "description": "Timed catalog sound cues for one children's story.",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "cues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "catalog_id": {"type": "string"},
                            "start": {"type": "number"},
                            "end": {"type": "number"},
                            "reason": {"type": "string"},
                        },
                        "required": ["catalog_id", "start", "end", "reason"],
                    },
                }
            },
            "required": ["cues"],
        },
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mix a local catalog SFX bed from an xAI plan.")
    parser.add_argument("--story", type=Path, default=DEFAULT_STORY)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    load_local_env()
    api_key = os.environ.get("XAI_API_KEY", "").strip()
    if not api_key:
        print(
            "XAI_API_KEY is not set. Copy scripts/local_sfx_test/.env.example "
            "to .env in the repo root or in scripts/local_sfx_test/.",
            file=sys.stderr,
        )
        return 2

    story = json.loads(args.story.read_text(encoding="utf-8"))
    catalog_ids = load_catalog_ids(_catalog_path())
    clips = resolve_clips(catalog_ids, _clip_dirs())
    compact = compact_story(story, catalog_ids)
    args.out.mkdir(parents=True, exist_ok=True)
    prompt_path = args.out / "xai_prompt.json"
    prompt_doc = {
        "model": os.environ.get("XAI_MODEL", "grok-4.7").strip() or "grok-4.7",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": compact},
        ],
    }
    prompt_path.write_text(json.dumps(prompt_doc, indent=2) + "\n", encoding="utf-8")

    model = prompt_doc["model"]
    base_url = os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1").strip() or "https://api.x.ai/v1"
    try:
        raw_plan = request_plan(api_key, base_url, model, compact)
    except httpx.HTTPError as exc:
        print(f"xAI request failed: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    accepted, dropped = validate_cues(raw_plan.get("cues"), set(catalog_ids), float(compact["duration_seconds"]))
    used, missing = stitch_bed(accepted, clips, float(compact["duration_seconds"]), args.out / "sfx_bed.mp3")
    plan_doc = {
        "model": model,
        "raw": raw_plan,
        "accepted": accepted,
        "dropped": dropped,
        "mixed": used,
        "missing_clips": missing,
        "clips": {slot_id: str(path) for slot_id, path in clips.items()},
    }
    (args.out / "xai_plan.json").write_text(json.dumps(plan_doc, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {prompt_path}")
    print(f"wrote {args.out / 'xai_plan.json'}")
    print(f"wrote {args.out / 'sfx_bed.mp3'} ({len(used)} clips)")
    if dropped:
        print(f"dropped {len(dropped)} cue(s)")
    if missing:
        print("missing local clips: " + ", ".join(missing), file=sys.stderr)
        return 1
    if not used:
        print("xAI returned no usable catalog cues.", file=sys.stderr)
        return 1
    return 0


def load_local_env() -> None:
    """Load repo .env, then a harness-local .env. Does not print values."""

    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise SystemExit(
            "python-dotenv is not installed. "
            'Run: pip install -r scripts/local_sfx_test/requirements.txt'
        ) from exc

    load_dotenv(ROOT / ".env")
    load_dotenv(HERE / ".env", override=True)


def load_catalog_ids(path: Path) -> list[str]:
    """Ids from catalog.json, skipping rejected slots. Falls back if the file is missing."""

    if not path.is_file():
        print(f"catalog not found at {path}; using the built-in id list", file=sys.stderr)
        return list(FALLBACK_CATALOG_IDS)
    catalog = json.loads(path.read_text(encoding="utf-8"))
    entries = catalog.get("entries") if isinstance(catalog, dict) else None
    if not isinstance(entries, list):
        print(f"{path} has no entries list; using the built-in id list", file=sys.stderr)
        return list(FALLBACK_CATALOG_IDS)
    ids = [
        str(entry["id"])
        for entry in entries
        if isinstance(entry, dict) and entry.get("id") and entry.get("status") != "rejected"
    ]
    return ids or list(FALLBACK_CATALOG_IDS)


def compact_story(payload: dict, catalog_ids: list[str]) -> dict:
    """Transcript, timings, and catalog ids. No Deepgram metadata and no FreeSound fields."""

    metadata = payload.get("metadata") or {}
    results = payload.get("results") or {}
    alternative: dict = {}
    channels = results.get("channels") or []
    if channels and isinstance(channels[0], dict):
        alternatives = channels[0].get("alternatives") or []
        if alternatives and isinstance(alternatives[0], dict):
            alternative = alternatives[0]
    words = []
    for word in alternative.get("words") or []:
        if not isinstance(word, dict):
            continue
        words.append(
            {
                "word": word.get("punctuated_word") or word.get("word") or "",
                "start": word.get("start"),
                "end": word.get("end"),
            }
        )
    utterances = []
    for utterance in results.get("utterances") or []:
        if not isinstance(utterance, dict):
            continue
        utterances.append(
            {
                "start": utterance.get("start"),
                "end": utterance.get("end"),
                "text": utterance.get("transcript") or "",
            }
        )
    duration = metadata.get("duration")
    if not isinstance(duration, (int, float)):
        ends = [word["end"] for word in words if isinstance(word.get("end"), (int, float))]
        duration = max(ends) if ends else 0.0
    return {
        "duration_seconds": duration,
        "transcript": alternative.get("transcript") or "",
        "words": words,
        "utterances": utterances,
        "available_catalog": list(catalog_ids),
    }


def resolve_clips(catalog_ids: list[str], directories: list[Path]) -> dict[str, Path]:
    """Map a catalog id to the first non-empty audio file named like that id."""

    found: dict[str, Path] = {}
    for slot_id in catalog_ids:
        for directory in directories:
            for suffix in CLIP_SUFFIXES:
                candidate = directory / f"{slot_id}{suffix}"
                if candidate.is_file() and candidate.stat().st_size > 0:
                    found[slot_id] = candidate
                    break
            if slot_id in found:
                break
    return found


def validate_cues(raw_cues: object, allowed: set[str], duration: float) -> tuple[list[dict], list[dict]]:
    """Keep cues whose catalog_id is in the pack and whose window sits on the story."""

    accepted: list[dict] = []
    dropped: list[dict] = []
    if not isinstance(raw_cues, list):
        return [], [{"reason": "cues was not a list"}]
    for cue in raw_cues:
        if not isinstance(cue, dict):
            dropped.append({"reason": "cue was not an object"})
            continue
        catalog_id = str(cue.get("catalog_id") or "").strip()
        if catalog_id not in allowed:
            dropped.append({"catalog_id": catalog_id, "reason": "not in catalog"})
            continue
        try:
            start = float(cue["start"])
            end = float(cue["end"])
        except (KeyError, TypeError, ValueError):
            dropped.append({"catalog_id": catalog_id, "reason": "missing start or end"})
            continue
        start = max(0.0, start)
        end = min(float(duration), end)
        if end - start < 0.05:
            dropped.append({"catalog_id": catalog_id, "reason": "window shorter than 50ms"})
            continue
        accepted.append(
            {
                "catalog_id": catalog_id,
                "start": round(start, 3),
                "end": round(end, 3),
                "reason": str(cue.get("reason") or "").strip(),
            }
        )
    return accepted, dropped


def stitch_bed(
    cues: list[dict],
    clips_by_id: dict[str, Path],
    duration_seconds: float,
    output_path: Path,
) -> tuple[list[str], list[str]]:
    """Overlay local clips on silence the length of the story. Returns mixed and missing ids."""

    timed: list[TimedClip] = []
    used: list[str] = []
    missing: list[str] = []
    for cue in cues:
        path = clips_by_id.get(cue["catalog_id"])
        if path is None:
            missing.append(cue["catalog_id"])
            continue
        timed.append(
            TimedClip(
                start_ms=int(round(float(cue["start"]) * 1000)),
                end_ms=int(round(float(cue["end"]) * 1000)),
                audio_bytes=path.read_bytes(),
                query=cue["catalog_id"],
            )
        )
        used.append(cue["catalog_id"])
    mix_sfx_mp3(timed, max(int(round(duration_seconds * 1000)), 1), output_path)
    return used, missing


def request_plan(api_key: str, base_url: str, model: str, compact: dict) -> dict:
    """POST /chat/completions and return the parsed cue object. The key is not logged."""

    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(compact, ensure_ascii=False)},
        ],
        "response_format": RESPONSE_FORMAT,
    }
    timeout = float(os.environ.get("HTTP_TIMEOUT_SECONDS", "120") or "120")
    with httpx.Client(timeout=timeout) as http:
        response = http.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    if response.status_code in {401, 403}:
        raise RuntimeError(f"xAI rejected XAI_API_KEY (HTTP {response.status_code}).")
    if response.status_code >= 400:
        raise RuntimeError(f"xAI request failed (HTTP {response.status_code}): {response.text[:400]}")
    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected xAI response shape: {exc}") from exc
    text = _message_text(content)
    parsed = _parse_json_object(text)
    if not isinstance(parsed, dict):
        raise RuntimeError("xAI did not return a JSON object")
    return parsed


def _catalog_path() -> Path:
    raw = os.environ.get("SFX_CATALOG_JSON", "").strip()
    return Path(raw) if raw else DEFAULT_CATALOG


def _clip_dirs() -> list[Path]:
    raw = os.environ.get("SFX_CATALOG_DIR", "").strip()
    primary = Path(raw) if raw else DEFAULT_CLIPS
    dirs = [primary]
    if PREVIEW_CLIPS not in dirs:
        dirs.append(PREVIEW_CLIPS)
    return dirs


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
        return "".join(parts)
    raise RuntimeError("xAI message content was not text")


def _parse_json_object(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned
        if cleaned.endswith("```"):
            cleaned = cleaned[: cleaned.rfind("```")]
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("xAI cue JSON could not be parsed") from None
        return json.loads(cleaned[start : end + 1])


if __name__ == "__main__":
    raise SystemExit(main())
