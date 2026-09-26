#!/usr/bin/env python3
"""Mix catalog sound effects onto a story recording.

Python sends the file to Deepgram (or Gladia when only GLADIA_API_KEY is set).
xAI only chooses which catalog ids match the words and when they play. Python
downloads those FreeSound previews and lays them on the recording at those times.
The output length is the recording.

    python scripts/render_story.py story.wav
    python scripts/render_story.py story.m4a -o out/story_with_sfx.mp3

Requires XAI_API_KEY and either DEEPGRAM_API_KEY or GLADIA_API_KEY. Catalog
slots need a preview_url (python scripts/build_sfx_catalog.py). Catalog-only
mode does not search FreeSound.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.schemas.deepgram import DeepgramTranscript  # noqa: E402
from app.services.deepgram import HttpDeepgramClient  # noqa: E402
from app.services.freesound import HttpFreeSoundClient  # noqa: E402
from app.services.pipeline import run_pipeline  # noqa: E402
from app.services.sfx_catalog import CATALOG_PATH, load_catalog  # noqa: E402
from app.services.transcribe import TranscriptionError, transcribe_audio  # noqa: E402
from app.services.xai import HttpXaiClient  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lay catalog sound effects onto a story recording.")
    parser.add_argument("audio", type=Path, help="Story recording (wav, mp3, m4a, ...).")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=ROOT / "out" / "story_with_sfx.mp3",
        help="Where to write the mixed MP3.",
    )
    args = parser.parse_args(argv)

    if not args.audio.is_file():
        print(f"Audio file not found: {args.audio}", file=sys.stderr)
        return 2

    settings = Settings(
        freesound_catalog_only=True,
        sfx_catalog_path=str(CATALOG_PATH),
    )
    if not settings.xai_api_key.strip():
        print("XAI_API_KEY is not set. Add it to .env (see .env.example).", file=sys.stderr)
        return 2
    if not settings.deepgram_api_key.strip() and not settings.gladia_api_key.strip():
        print(
            "Set DEEPGRAM_API_KEY or GLADIA_API_KEY. No request was sent.",
            file=sys.stderr,
        )
        return 2

    ready = _catalog_preview_count(CATALOG_PATH)
    if ready == 0:
        print(
            f"{CATALOG_PATH} has no preview_url values, so no catalog clip can be downloaded. "
            "Run: python scripts/build_sfx_catalog.py",
            file=sys.stderr,
        )
        return 2

    audio = args.audio.read_bytes()
    mime = mimetypes.guess_type(args.audio.name)[0] or "application/octet-stream"
    deepgram = HttpDeepgramClient(settings)
    xai = HttpXaiClient(settings)
    freesound = HttpFreeSoundClient(settings)
    try:
        try:
            raw = transcribe_audio(settings, deepgram, audio, mime, args.audio.name)
            transcript = DeepgramTranscript.model_validate(raw).normalized()
        except (TranscriptionError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        output = run_pipeline(transcript, xai, freesound, story_bytes=audio)
    finally:
        deepgram.close()
        xai.close()
        freesound.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(output.audio_bytes)
    plan_path = args.output.with_suffix(".json")
    plan_path.write_text(
        json.dumps(
            {
                "audio": str(args.audio),
                "catalog": str(CATALOG_PATH),
                "duration_seconds": output.duration_seconds,
                "warnings": output.warnings,
                "cues": [
                    {
                        "catalog_id": cue.catalog_id,
                        "query": cue.query,
                        "description": cue.description,
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
        print(f"  {cue.start:.2f}-{cue.end:.2f}s  {cue.catalog_id}")
    if output.warnings:
        print("warnings:")
        for warning in output.warnings:
            print(f"  {warning}")
    print(f"wrote {args.output}")
    print(f"wrote {plan_path}")
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
