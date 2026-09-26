# Local xAI + catalog SFX smoke test

This folder plans a sound bed for one story without running the API or searching FreeSound. xAI sees a short prompt: the transcript, word times, utterances, and the catalog **ids** (the same strings as the clip filenames). It must return those ids with start, end, and a reason. The script overlays the matching local files on silence as long as the story.

## Setup

From the repo root:

```bash
pip install -e ".[dev]"
pip install -r scripts/local_sfx_test/requirements.txt
cp scripts/local_sfx_test/.env.example .env
```

Put `XAI_API_KEY` in that `.env` (repo root or `scripts/local_sfx_test/.env`). Optional: `XAI_MODEL` (default `grok-4.7`), `XAI_BASE_URL` (default `https://api.x.ai/v1`), `SFX_CATALOG_DIR`, `SFX_CATALOG_JSON`.

## Clips

Copy preview files into `catalog_clips/` using the catalog id as the filename (`rain.mp3`, `door-creak.wav`, …). See `catalog_clips/README.txt`. Those audio files are gitignored. If a name is missing here, the script also looks in `assets/sfx_catalog/previews/`.

`deepgram_story.json` is a Deepgram-shaped bedtime story (about 56 seconds) that mentions rain, a creaky door, a bird, a dog, wooden footsteps, wind, thunder, a page turn, a magic chime, a kettle, an owl, and a yawn.

## Run

```bash
python scripts/local_sfx_test/run_local_sfx_test.py
```

Outputs (gitignored) land in `scripts/local_sfx_test/out/`:

- `xai_prompt.json` — system text and the compact user payload. The API key is not written.
- `xai_plan.json` — model cues, which ids were kept, and which clips were missing.
- `sfx_bed.mp3` — silent timeline of the story duration with the chosen clips.

The script exits with an error if the key is missing, xAI fails, or a kept cue has no local file.
