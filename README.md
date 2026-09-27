# HackGT TED story backend

Grandparents record a story for a child. Upload that audio and the service returns the same recording with picture-book sound effects laid on top, at the words they belong to.

Deepgram (or Gladia, if that is the key you have) timestamps the words. xAI only decides which sounds in the fixed catalog match those words, and when. Python downloads those FreeSound previews and mixes them onto the recording. The output is one MP3 the length of the upload.

A separate path still accepts Deepgram JSON and stores an effects-only track in Supabase. That path is for the kids app that plays effects under a recording it already has. The file you want from an upload is `POST /stories/render`.

```
audio file
  -> Deepgram POST /v1/listen  (or Gladia if DEEPGRAM_API_KEY is empty)
  -> word timestamps
  -> xAI picks catalog ids (similarity only)
  -> Python downloads only those FreeSound previews
  -> Python lays the clips on the recording at those times
  -> MP3, same length as the upload
```

## Requirements

- Python 3.11 or 3.12 (3.13 is supported via the `audioop-lts` dependency declared in `pyproject.toml`)
- [ffmpeg](https://ffmpeg.org/) on `PATH`, built with libmp3lame, for local mixes. pydub uses it to write the SFX file. If `ffmpeg` is missing, the app falls back to the `imageio-ffmpeg` binary shipped with the Python dependencies (this is what the Vercel function uses).

```bash
# Debian/Ubuntu
sudo apt-get install ffmpeg
# macOS
brew install ffmpeg
```

## Setup

From the repo root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Edit `.env` and set `XAI_API_KEY`, `FREESOUND_API_KEY`, `SUPABASE_URL`, and `SUPABASE_SERVICE_ROLE_KEY`. Do not commit `.env`. The sample file has empty values only.

Run `supabase/schema.sql` in the Supabase SQL editor once. It creates `public.sfx_catalog` (the catalog JSON), a minimal `public.recordings` table for mixed MP3 URLs, and a public Storage bucket named `story-sfx`. If an older wide `recordings` table is already there, drop it before re-running. Do not add marketplace tables to this project.

```bash
uvicorn app.main:app --reload
```

The API listens on `http://127.0.0.1:8000`. `uvicorn main:app` loads the same app. Interactive docs are at `/docs`. `GET /health` returns `{"status": "ok"}` with no keys configured. `POST /stories/render` returns **503** until xAI and a transcriber key are set. It does not need Supabase. Sound effects come from the checked-in catalog, so a FreeSound API key is not required on the request path.

The mix step writes an MP3 under the system temp directory (`/tmp` on Linux and on Vercel) and deletes that file after the bytes are uploaded. The recording row stores `sfx_storage_path` and the public `sfx_url`. Transcript text, cues, and warnings sit in a `meta` jsonb column on that same minimal row.

## Environment

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `XAI_API_KEY` | to process stories | empty | Bearer token for xAI Chat Completions. |
| `XAI_BASE_URL` | no | `https://api.x.ai/v1` | OpenAI-compatible base URL. |
| `XAI_MODEL` | no | `grok-4.7` | Chat model id. `grok-4.7` supports structured outputs. |
| `XAI_IMAGE_MODEL` | no | `grok-imagine-image-2.0` | Grok Imagine model for `POST /stories/cover`. |
| `FREESOUND_API_KEY` | to build the catalog | empty | FreeSound APIv2 token for `scripts/build_sfx_catalog.py`. Not sent on catalog-only story requests. |
| `FREESOUND_BASE_URL` | no | `https://freesound.org` | API host for the builder and for live search. |
| `FREESOUND_CATALOG_ONLY` | no | `true` | Match cues to `assets/sfx_catalog/catalog.json` and download that preview. `false` searches FreeSound per cue. |
| `SFX_CATALOG_PATH` | no | repo catalog | Override the catalog JSON path. |
| `SUPABASE_URL` | to store stories | empty | Project URL, `https://<ref>.supabase.co`. |
| `SUPABASE_SERVICE_ROLE_KEY` | to store stories | empty | Server-side key. Bypasses RLS. Never send it to a browser. |
| `SUPABASE_SFX_BUCKET` | no | `story-sfx` | Public Storage bucket for SFX MP3s. |
| `DEEPGRAM_API_KEY` | to transcribe audio | empty | `Authorization: Token` for `POST /v1/listen`. Preferred for `/stories/render`. JSON process does not need it. |
| `DEEPGRAM_BASE_URL` | no | `https://api.deepgram.com` | Listen API host. |
| `DEEPGRAM_MODEL` | no | `nova-3` | Prerecorded model id. |
| `DEEPGRAM_LANGUAGE` | no | `en` | Language hint passed to Deepgram. |
| `GLADIA_API_KEY` | if Deepgram is unset | empty | Fallback transcriber for `/stories/render`. Ignored when `DEEPGRAM_API_KEY` is set. |
| `CORS_ORIGINS` | no | `*` | Comma-separated browser origins for the web and kids apps. |
| `HTTP_TIMEOUT_SECONDS` | no | `60` | Timeout for Deepgram and FreeSound. xAI cue planning waits at least 180 seconds, or this value when it is higher. |

A missing `XAI_API_KEY` or Supabase key raises before any external call that needs it. Supabase is checked when the recording row is created, so a missing project URL returns **503** before xAI is called. The catalog read is separate: with no Supabase keys, or when the active row is missing, the API uses `assets/sfx_catalog/catalog.json`. After a row exists, a failed xAI or FreeSound call is stored as `status: "failed"` and the response includes `recording_id`. `FREESOUND_API_KEY` is required only to fill the catalog, or when `FREESOUND_CATALOG_ONLY=false`.

`SUPABASE_ANON_KEY` is not used. Browser and kids apps should call this API and then load `sfx_url`. That URL is the public object URL for bucket `story-sfx`.

## Backend and frontend Supabase

This FastAPI backend and the Next.js app do not share a client or a set of env vars.

- This service uses `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` to read and write the `sfx_catalog` JSON, and to upload mixed MP3s to the `story-sfx` bucket. The service role key stays on the server.
- The Next.js frontend (`ted-web`) uses `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` through `@supabase/ssr`. That client work lands in a separate ted-web pull request. It does not belong in this Python repo.
- Marketplace and social data are frontend database scope. Do not add those tables here.

## API

### `POST /stories/render`

Upload a recording. The response body is that recording with the effects mixed in (`audio/mpeg`). Supabase is not used.

```bash
curl -sS -X POST http://127.0.0.1:8000/stories/render \
  -F "audio=@story.wav;type=audio/wav" \
  -o out/story_with_sfx.mp3
```

A story recorded in several moments can be sent in one request: repeat the `audio` field once per moment, in playback order. The server joins them into one recording with a 300 ms pause between moments, then transcribes and mixes that. The 25 MB limit covers all the files together. A file that cannot be decoded returns **422** naming it (`Audio file 2 could not be decoded`). A single `audio` field works exactly as before.

```bash
curl -sS -X POST http://127.0.0.1:8000/stories/render \
  -F "audio=@moment-1.m4a;type=audio/m4a" \
  -F "audio=@moment-2.m4a;type=audio/m4a" \
  -o out/story_with_sfx.mp3
```

Needs `XAI_API_KEY` and `DEEPGRAM_API_KEY` (or `GLADIA_API_KEY`). The decoded file length is the clock, so a cue cannot run past the recording. Each effect starts about 150 ms after its cue time, so a bark does not lead the word, and its level follows the narration in that window so it stays under the voice. Response headers: `X-Story-Duration-Seconds`, `X-Story-Cue-Count`, and `X-Story-Warnings` when a cue was skipped.

The same job from a file on disk:

```bash
python scripts/render_story.py story.wav -o out/story_with_sfx.mp3
```

That also writes `out/story_with_sfx.json` with the cue times, the transcript, a one-sentence description, and a few hashtags. The description is a second xAI call. It does not change which sounds are mixed. `out/` is gitignored.

### `POST /stories/describe`

Transcribe a recording and return catalog metadata. Does not plan cues or mix audio.

Multipart `audio` (25 MB max; repeat the field for several moments, joined the same way as `/stories/render`), or JSON `{"url": "https://.../story.mp3"}`.

```bash
curl -sS -X POST http://127.0.0.1:8000/stories/describe \
  -F "audio=@story.mp3;type=audio/mpeg"
```

```json
{
  "audio": "story.mp3",
  "duration_seconds": 17.232,
  "transcript_text": "The dog barked once.",
  "description": "A short outdoor story where a dog barks.",
  "hashtags": ["animals", "calm"]
}
```

`description` is one sentence. `hashtags` is one to three of: `spooky`, `calm`, `funny`, `adventure`, `bedtime`, `animals`, `nature`, `family`, `magic`. Needs `XAI_API_KEY` and a transcriber key (`DEEPGRAM_API_KEY`, or `GLADIA_API_KEY` for an upload). A JSON `url` is fetched by Deepgram.

### `POST /stories/cover`

Same audio input as `/stories/describe`. After the transcript, one xAI call writes the description, hashtags, and a single picture moment. Grok Imagine (`grok-imagine-image-2.0`) then draws a square picture-book cover from that card. The style instructions are fixed: gouache and colored pencil, playful, child-friendly, and not photoreal. Does not plan cues or mix audio.

```bash
curl -sS -X POST http://127.0.0.1:8000/stories/cover \
  -F "audio=@story.mp3;type=audio/mpeg"
```

The JSON matches `/stories/describe`, plus `scene` and `image_url`. The image URL is temporary, so save the file if the catalog should keep it. Override the model with `XAI_IMAGE_MODEL`.

### `POST /stories/transcribe`

Transcribe audio and return the normalized transcript. Does not mix sound effects.

JSON body: `{"url": "https://.../story.wav"}`. Or multipart form data with an `audio` file (25 MB max). The server calls `POST https://api.deepgram.com/v1/listen` with `model=nova-3` (override with `DEEPGRAM_MODEL`), `language`, `smart_format`, `punctuate`, `utterances`, and `paragraphs`. Auth is `Authorization: Token $DEEPGRAM_API_KEY`.

The response includes `transcript_text`, `duration_seconds`, `words` (`word`, `start`, `end`), `segments`, and the raw `deepgram` document.

### `POST /stories/process-audio`

Same audio input as `/stories/transcribe`, plus optional `story_id`, `title`, `narrator`, and `source_audio_url`. When the body is JSON, `url` is stored as `source_audio_url` unless another value is set. The handler transcribes, then runs the same SFX pipeline as `/stories/process`.

### `POST /stories/process`

Body: Deepgram JSON under `deepgram`, plus optional `story_id`, `title`, `narrator`, and `source_audio_url`. A raw Deepgram document (top-level `results` or `transcript`) is accepted too.

```bash
jq -n --slurpfile dg fixtures/deepgram_sample.json \
  '{title:"Rain story", story_id:"demo-1", narrator:"Grandma", deepgram:$dg[0]}' \
  | curl -sS -X POST http://127.0.0.1:8000/stories/process \
      -H 'Content-Type: application/json' -d @-
```

`201` response includes `id`, `status` (`ready`), cue timestamps, `warnings`, and `sfx_url` (a public Supabase Storage URL).

Failed external calls are stored as `status: "failed"` and returned as:

- **503** — missing or rejected API key
- **502** — xAI failed or returned an unusable completion
- **429** — FreeSound rate-limited every cue
- **422** — the transcript JSON could not be read
- **500** — ffmpeg/pydub could not write the MP3

A single FreeSound miss (no results, or one preview that will not decode) is a warning. The other cues are still mixed, and the story stays `ready`.

### `GET /stories`

List recordings, newest first. Optional `?story_id=` filter. Each item includes `sfx_url` when the mix is ready.

### `GET /stories/{id}`

Metadata, transcript text, the original Deepgram JSON, cue timestamps, warnings, and `sfx_url`.

### `GET /stories/{id}/sfx`

Redirects (`307`) to the public Storage URL. The file is silence plus the placed effects. It does not contain the grandparent's voice. The kids app can also use `sfx_url` from the JSON and skip this redirect.

### `GET /health`

Liveness check.

## xAI assumptions

Cue planning uses xAI's [OpenAI-compatible Chat Completions API](https://docs.x.ai/developers/model-capabilities/legacy/chat-completions).

- Request: `POST {XAI_BASE_URL}/chat/completions`
- Default URL: `https://api.x.ai/v1/chat/completions`
- Default model: `grok-4.7`, the chat model on the [Grok 4.7 model page](https://docs.x.ai/docs/models/grok-4.7). That page lists structured outputs as supported. Override with `XAI_MODEL`.
- Auth header: `Authorization: Bearer $XAI_API_KEY`
- Structured cues: `response_format.type = "json_schema"` with the `sfx_plan` schema (`catalog_id`, `description`, `start`, `end` in seconds). `catalog_id` must be one of the ids in the active catalog (the Supabase row, or `assets/sfx_catalog/catalog.json` when that row is not used). The client still accepts fenced JSON or a JSON object wrapped in prose if the message is not bare JSON.
- A missing key fails in-process with `XaiNotConfiguredError` and does not open a socket.
- `grok-4.7` reasons by default. Cue planning waits at least 180 seconds even when `HTTP_TIMEOUT_SECONDS` is 60. Set `HTTP_TIMEOUT_SECONDS` above 180 if a long story still times out.

xAI only chooses which catalog sounds are similar to the story and when they play. It does not download audio and it does not mix. Python drops any id that is not in the catalog, fetches those preview files, and places them on the recording.

The planner is asked for at most one cue per sentence, and only when that sentence clearly names the sound. Python drops any extra cue that lands in a sentence that already has one. Cue windows are then clamped to the story duration. Windows shorter than 50ms are dropped. Playback starts about 150 ms after the cue's start time.

Example cue after alignment:

```json
{
  "query": "rain",
  "catalog_id": "rain",
  "description": "Rain under the first sentence.",
  "start": 0.75,
  "end": 4.21,
  "start_ms": 750,
  "end_ms": 4210
}
```

## FreeSound catalog

Story requests use a fixed pack of picture-book sounds (about 30–50 slots: animals, weather, home, footsteps, doors, magic, bedtime beats). On each request the API loads the active `sfx_catalog` row (`id = 'active'`, `payload` jsonb) when `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are set. If that row is missing, the payload is invalid, or Supabase is not configured, it uses `assets/sfx_catalog/catalog.json`. `SFX_CATALOG_PATH` pins a file and skips Supabase. After xAI returns catalog ids, Python downloads only those entries' `preview_url` values. It does not search FreeSound while `FREESOUND_CATALOG_ONLY` is true (the default).

To copy the checked-in file into Supabase after you fill it:

```bash
python scripts/build_sfx_catalog.py --push
```

`--push` still writes `catalog.json`, then upserts `public.sfx_catalog` and increments `version`. That payload includes `loudness_target_lufs`, `sfx_level_db`, and a `gain_db` on each filled slot. The same helper is `save_active_catalog` / `push_checked_in_catalog` in `app/services/catalog_store.py`. Production reads this row, so a push is what makes the shared clip volume live.

The checked-in file lists the slots with empty FreeSound ids. Fill them once:

```bash
python scripts/build_sfx_catalog.py
```

That needs `FREESOUND_API_KEY`. It picks one sound per empty or rejected slot (rating, downloads, CC0 then Attribution, duration) and writes preview MP3s to `assets/sfx_catalog/previews/` so you can listen. Those MP3s are gitignored. Details, including how to set `approved` or `rejected` and how to swap a bad id, are in `assets/sfx_catalog/README.md`.

A cue that matches a slot with no `preview_url` is skipped with a warning that names the builder. A cue that matches nothing in the catalog is skipped the same way. Pending slots that already have a preview URL are used. Rejected slots are ignored.

The builder measures each preview's integrated loudness and stores `gain_db` so every clip lands at `loudness_target_lufs` (-20 LUFS). It writes that leveled MP3 to `assets/sfx_catalog/previews/<id>.mp3` (the raw download stays in `previews/source/`). The mixer downloads the original FreeSound URL and applies the same `gain_db`, then adds `sfx_level_db` from the catalog document. That one number turns every effect up or down together. `0` plays the matched preview level. A slot with no `gain_db` yet is measured from the downloaded bytes on that request. `download_url` in the catalog is FreeSound's original-file endpoint and needs OAuth2, which v1 does not implement. HTTP 429 on a preview download is retried up to three times using `Retry-After` or a short backoff. If every cue is still rate-limited, the request returns **429**. Create a token at <https://freesound.org/apiv2/apply>.

Set `FREESOUND_CATALOG_ONLY=false` only if you want the old per-cue text search (`GET /apiv2/search/text/`, `Authorization: Token` on the API host, first preview-bearing hit). Catalog mode does not send the token to the preview CDN.

## Render a recording

```bash
python scripts/render_story.py path/to/story.wav -o out/story_with_sfx.mp3
```

Or `POST /stories/render` with a multipart `audio` field. Both run the same pipeline: transcribe the file, let xAI pick catalog ids, download those previews, mix them onto the file.

`fixtures/deepgram_sample.json` is a Deepgram listen document for a bedtime story about 56 seconds long. `POST /stories/process` still accepts that JSON under `deepgram` and stores an effects-only MP3. Catalog slots need a `preview_url`; fill those with `python scripts/build_sfx_catalog.py` if they are still empty.

## Deepgram

Direct transcription uses the [prerecorded listen API](https://developers.deepgram.com/docs/pre-recorded-audio):

- `POST {DEEPGRAM_BASE_URL}/v1/listen`
- `Authorization: Token $DEEPGRAM_API_KEY`
- Default model `nova-3`
- Query flags `smart_format`, `punctuate`, `utterances`, and `paragraphs` so the response includes word and segment timestamps

`POST /stories/process` still accepts that JSON without calling Deepgram. Both paths go through the same normalizer.

## Deepgram JSON

`fixtures/deepgram_sample.json` is a prerecorded listen response for that bedtime story: `metadata.duration`, `results.channels[0].alternatives[0]` (`transcript`, `words` with `start`/`end`/`punctuated_word`, paragraph sentences), and `results.utterances`. Unknown Deepgram fields are ignored.

A smaller document also works:

```json
{
  "transcript": "The door creaked.",
  "duration": 3,
  "words": [
    {"word": "the", "start": 0.0, "end": 0.2, "punctuated_word": "The"},
    {"word": "door", "start": 0.2, "end": 0.6},
    {"word": "creaked", "start": 0.6, "end": 1.2, "punctuated_word": "creaked."}
  ]
}
```

Story length is `metadata.duration` when that is longer than the last word, so trailing silence stays in the SFX track. Each clip is trimmed to its cue window so a long preview cannot spill into the next sentence. The timeline is padded or trimmed to that duration before ffmpeg encodes the MP3. Encoder framing can add a few dozen milliseconds around that exact length.

## Deploy on Vercel

The FastAPI app is one Python function. Vercel loads `app` from `app/main.py` via `[tool.vercel] entrypoint = "app.main:app"` in `pyproject.toml`. `vercel.json` sets `maxDuration` to 60 seconds because cue planning, FreeSound downloads, and the mix can outlast the platform default.

1. Create a Supabase project and run `supabase/schema.sql` in the SQL editor. That creates the catalog table. Marketplace data stays in the frontend database.
2. Import this repo as a Vercel project. The Python runtime picks up FastAPI from `pyproject.toml`.
3. In the Vercel project settings, set the same variables as `.env.example`:
   - `XAI_API_KEY`, `XAI_BASE_URL`, `XAI_MODEL`
   - `FREESOUND_API_KEY` and `FREESOUND_BASE_URL` if you are not shipping a filled catalog, plus `FREESOUND_CATALOG_ONLY` (default true) and optional `SFX_CATALOG_PATH`
   - `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` so the function can fetch and store the catalog JSON, plus `SUPABASE_SFX_BUCKET` for mixed MP3s
   - `DEEPGRAM_API_KEY`, `DEEPGRAM_BASE_URL`, `DEEPGRAM_MODEL`, `DEEPGRAM_LANGUAGE` when audio is transcribed on the server
   - `CORS_ORIGINS` for the web and kids app origins
   - `HTTP_TIMEOUT_SECONDS` if `grok-4.7` needs longer than 60 seconds (the function `maxDuration` must be at least that long)
4. Deploy. `GET /health` should return `{"status": "ok"}`.

The function filesystem is ephemeral. Mixes use the temp directory and are uploaded to Storage before the response returns. Do not put `SUPABASE_SERVICE_ROLE_KEY` in a client bundle.

## Tests

External calls are mocked. No API keys and no network:

```bash
pytest
```

`tests/test_sample_pipeline.py` posts a WAV to `POST /stories/render`. Transcription, xAI, and FreeSound are faked. The mixer is real: the effect stays under the narration and begins slightly after the cue, the MP3 length follows the upload (not a longer transcript duration), and the FreeSound client is asked only for that catalog preview URL.

`tests/test_mixer.py` checks that alignment on the PCM timeline, before MP3 export, and the effects-only track used by `/stories/process`.

## Layout

```
main.py                     uvicorn main:app (re-exports app.main:app)
app/main.py                 create_app
app/config.py               pydantic-settings
app/schemas/deepgram.py     Deepgram JSON -> transcript
app/schemas/sfx.py          cue schema and alignment
app/services/xai.py         xAI Chat Completions client
app/services/freesound.py   catalog preview download
app/services/sfx_catalog.py catalog load, match, and builder ranking
app/services/transcribe.py  Deepgram, or Gladia when that key is the one set
app/services/mixer.py       effects on the recording, or an effects-only track
app/services/pipeline.py    planning, download, and mix
app/services/deepgram.py    prerecorded POST /v1/listen
DeepGram.py                 Gladia upload + poll
assets/sfx_catalog/catalog.json  fixed kids-book sound slots
scripts/build_sfx_catalog.py     one-shot FreeSound fill for those slots
scripts/render_story.py          audio file -> mixed MP3
app/services/store.py       Supabase table + Storage
app/api/routes.py           /stories/render, /stories/describe, /stories/cover, /stories/process, /health
supabase/schema.sql         recordings table and story-sfx bucket
fixtures/deepgram_sample.json
```
