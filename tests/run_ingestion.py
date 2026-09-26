#!/usr/bin/env python3
"""Turn fixtures/deepgram_sample.json into a timestamped SFX MP3.

This is the local story smoke path. Python reads the Deepgram JSON and asks
xAI with the same immersive prompt the API uses (scene beds, pause gaps, and
layered cues). Python then downloads those FreeSound previews from
assets/sfx_catalog/catalog.json and overlays every cue, including overlaps,
on a silent timeline the length of the story.

    python tests/run_ingestion.py
    python tests/run_ingestion.py --output out/story_sfx.mp3

Requires XAI_API_KEY in the environment or in .env. Each catalog slot needs a
preview_url (run scripts/build_sfx_catalog.py once). Catalog-only mode does
not search FreeSound.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.schemas.deepgram import DeepgramTranscript  # noqa: E402
from app.services.freesound import HttpFreeSoundClient  # noqa: E402
from app.services.pipeline import run_pipeline  # noqa: E402
from app.services.sfx_catalog import CATALOG_PATH, load_catalog  # noqa: E402
from app.services.xai import HttpXaiClient  # noqa: E402

SAMPLE = ROOT / "fixtures" / "deepgram_sample.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mix an SFX MP3 from the sample Deepgram story.")
    parser.add_argument("--story", type=Path, default=SAMPLE, help="Deepgram JSON to ingest.")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "out" / "story_sfx.mp3",
        help="Where to write the SFX MP3.",
    )
    args = parser.parse_args(argv)

    settings = Settings(
        freesound_catalog_only=True,
        sfx_catalog_path=str(CATALOG_PATH),
    )
    if not settings.xai_api_key.strip():
        print("XAI_API_KEY is not set. Add it to .env (see .env.example).", file=sys.stderr)
        return 2

    ready = _catalog_preview_count(CATALOG_PATH)
    if ready == 0:
        print(
            f"{CATALOG_PATH} has no preview_url values, so no catalog clip can be downloaded. "
            "Run: python scripts/build_sfx_catalog.py",
            file=sys.stderr,
        )
        return 2

    payload = json.loads(args.story.read_text(encoding="utf-8"))
    transcript = DeepgramTranscript.model_validate(payload).normalized()
    xai = HttpXaiClient(settings)
    freesound = HttpFreeSoundClient(settings)
    try:
        output = run_pipeline(transcript, xai, freesound)
    finally:
        xai.close()
        freesound.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(output.audio_bytes)
    plan_path = args.output.with_suffix(".json")
    plan_path.write_text(
        json.dumps(
            {
                "story": str(args.story),
                "catalog": str(CATALOG_PATH),
                "duration_seconds": output.duration_seconds,
                "warnings": output.warnings,
                "cues": [
                    {
                        "catalog_id": cue.catalog_id,
                        "query": cue.query,
                        "description": cue.description,
                        "reason": cue.reason,
                        "kind": cue.kind,
                        "gain_db": cue.gain_db,
                        "end_at_scene_change": cue.end_at_scene_change,
                        "until_seconds": cue.until_seconds,
                        "start": cue.start,
                        "end": cue.end,
                        "start_ms": cue.start_ms,
                        "end_ms": cue.end_ms,
                    }
                    for cue in output.cues
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"story duration: {output.duration_seconds:.2f}s")
    print(f"cues: {len(output.cues)}")
    for cue in output.cues:
        print(f"  {cue.start:.2f}-{cue.end:.2f}s  {cue.kind}  {cue.catalog_id}")
    if output.warnings:
        print("warnings:")
        for warning in output.warnings:
            print(f"  {warning}")
    print(f"wrote {args.output}")
    print(f"wrote {plan_path}")
    if output.warnings and not output.audio_bytes:
        return 1
    return 0


def _catalog_preview_count(path: Path) -> int:
    catalog = load_catalog(path)
    return sum(
        1
        for entry in catalog["entries"]
        if isinstance(entry, dict)
        and isinstance(entry.get("preview_url"), str)
        and entry["preview_url"].startswith("http")
        and entry.get("status") != "rejected"
    )


if __name__ == "__main__":
    raise SystemExit(main())
