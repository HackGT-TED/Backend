# HackGT TED story backend

Grandparents record a story for a child. Deepgram turns that recording into JSON with word and segment timestamps. This service asks xAI where the sound effects should go, downloads matching clips from FreeSound, and mixes an **SFX-only MP3** aligned to those timestamps. The grandparents' web app and the kids' app load the stored recording and the MP3.

Audio can be transcribed here with Deepgram, or the client can send Deepgram JSON it already fetched. Processing is synchronous: the mix finishes before the response. The MP3 is mixed in a temporary directory and uploaded to Supabase Storage. Metadata lives in a Supabase Postgres table. There is no local database.

```
audio URL or bytes -> Deepgram POST /v1/listen
  or Deepgram JSON the client already has
  -> transcript text + word/segment timestamps
  -> xAI chat completions (structured SFX cues)
  -> fixed SFX catalog match + that preview MP3 per cue
  -> silent timeline mixed under /tmp
  -> Supabase Storage object + recordings row
  -> public SFX URL
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

Run `supabase/schema.sql` in the Supabase SQL editor once. It creates `public.recordings` and a public Storage bucket named `story-sfx`.

```bash
uvicorn app.main:app --reload
```

The API listens on `http://127.0.0.1:8000`. Interactive docs are at `/docs`. `GET /health` returns `{"status": "ok"}` with no keys configured. `POST /stories/process` returns **503** until Supabase and xAI are configured. Sound effects come from the checked-in catalog, so a FreeSound API key is not required on the request path.

The mix step writes an MP3 under the system temp directory (`/tmp` on Linux and on Vercel) and deletes that file after the bytes are uploaded. The recording row stores `sfx_storage_path` and the public `sfx_url`.

## Environment

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `XAI_API_KEY` | to process stories | empty | Bearer token for xAI Chat Completions. |
| `XAI_BASE_URL` | no | `https://api.x.ai/v1` | OpenAI-compatible base URL. |
| `XAI_MODEL` | no | `grok-4.7` | Chat model id. `grok-4.7` supports structured outputs. |
| `FREESOUND_API_KEY` | to build the catalog | empty | FreeSound APIv2 token for `scripts/build_sfx_catalog.py`. Not sent on catalog-only story requests. |
| `FREESOUND_BASE_URL` | no | `https://freesound.org` | API host for the builder and for live search. |
| `FREESOUND_CATALOG_ONLY` | no | `true` | Match cues to `assets/sfx_catalog/catalog.json` and download that preview. `false` searches FreeSound per cue. |
| `SFX_CATALOG_PATH` | no | repo catalog | Override the catalog JSON path. |
| `SUPABASE_URL` | to store stories | empty | Project URL, `https://<ref>.supabase.co`. |
| `SUPABASE_SERVICE_ROLE_KEY` | to store stories | empty | Server-side key. Bypasses RLS. Never send it to a browser. |
| `SUPABASE_SFX_BUCKET` | no | `story-sfx` | Public Storage bucket for SFX MP3s. |
| `DEEPGRAM_API_KEY` | to transcribe audio | empty | `Authorization: Token` for `POST /v1/listen`. JSON process does not need it. |
| `DEEPGRAM_BASE_URL` | no | `https://api.deepgram.com` | Listen API host. |
| `DEEPGRAM_MODEL` | no | `nova-3` | Prerecorded model id. |
| `DEEPGRAM_LANGUAGE` | no | `en` | Language hint passed to Deepgram. |
| `CORS_ORIGINS` | no | `*` | Comma-separated browser origins for the web and kids apps. |
| `HTTP_TIMEOUT_SECONDS` | no | `60` | Timeout for xAI and FreeSound. Raise this if `grok-4.7` reasoning runs long. |

A missing `XAI_API_KEY` or Supabase key raises before any external call that needs it. Supabase is checked when the recording row is created, so a missing project URL returns **503** before xAI is called. After a row exists, a failed xAI or FreeSound call is stored as `status: "failed"` and the response includes `recording_id`. `FREESOUND_API_KEY` is required only to fill the catalog, or when `FREESOUND_CATALOG_ONLY=false`.

`SUPABASE_ANON_KEY` is not used. Browser and kids apps should call this API and then load `sfx_url`. That URL is the public object URL for bucket `story-sfx`.

## API

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
- Structured cues: `response_format.type = "json_schema"` with the `sfx_plan` schema (`query`, `description`, `start`, `end` in seconds). The client still accepts fenced JSON or a JSON object wrapped in prose if the message is not bare JSON.
- A missing key fails in-process with `XaiNotConfiguredError` and does not open a socket.
- `grok-4.7` reasons by default. The default HTTP timeout is 60 seconds; set `HTTP_TIMEOUT_SECONDS` higher if planning calls time out.

The planner is asked for at most eight child-friendly foley or ambience cues aligned to the word timestamps. Cue windows are then clamped to the story duration. Windows shorter than 50ms are dropped.

Example cue after alignment:

```json
{
  "query": "gentle rain ambience",
  "description": "Rain under the first sentence.",
  "start": 1.28,
  "end": 2.6,
  "start_ms": 1280,
  "end_ms": 2600
}
```

## FreeSound catalog

Story requests use a fixed pack of picture-book sounds in `assets/sfx_catalog/catalog.json` (about 30–50 slots: animals, weather, home, footsteps, doors, magic, bedtime beats). Each xAI cue is matched to the closest slot by keywords. The service downloads that slot's `preview_url` and does not call FreeSound search while `FREESOUND_CATALOG_ONLY` is true (the default).

The checked-in file lists the slots with empty FreeSound ids. Fill them once:

```bash
python scripts/build_sfx_catalog.py
```

That needs `FREESOUND_API_KEY`. It picks one sound per empty or rejected slot (rating, downloads, CC0 then Attribution, duration) and writes preview MP3s to `assets/sfx_catalog/previews/` so you can listen. Those MP3s are gitignored. Details, including how to set `approved` or `rejected` and how to swap a bad id, are in `assets/sfx_catalog/README.md`.

A cue that matches a slot with no `preview_url` is skipped with a warning that names the builder. A cue that matches nothing in the catalog is skipped the same way. Pending slots that already have a preview URL are used. Rejected slots are ignored.

Preview MP3s are what this service mixes. `download_url` in the catalog is FreeSound's original-file endpoint and needs OAuth2, which v1 does not implement. HTTP 429 on a preview download is retried up to three times using `Retry-After` or a short backoff. If every cue is still rate-limited, the request returns **429**. Create a token at <https://freesound.org/apiv2/apply>.

Set `FREESOUND_CATALOG_ONLY=false` only if you want the old per-cue text search (`GET /apiv2/search/text/`, `Authorization: Token` on the API host, first preview-bearing hit). Catalog mode does not send the token to the preview CDN.

## Deepgram

Direct transcription uses the [prerecorded listen API](https://developers.deepgram.com/docs/pre-recorded-audio):

- `POST {DEEPGRAM_BASE_URL}/v1/listen`
- `Authorization: Token $DEEPGRAM_API_KEY`
- Default model `nova-3`
- Query flags `smart_format`, `punctuate`, `utterances`, and `paragraphs` so the response includes word and segment timestamps

`POST /stories/process` still accepts that JSON without calling Deepgram. Both paths go through the same normalizer.

## Deepgram JSON

`fixtures/deepgram_sample.json` is a realistic prerecorded response: `metadata.duration`, `results.channels[0].alternatives[0]` (`transcript`, `words` with `start`/`end`/`punctuated_word`, paragraph sentences), and `results.utterances`. Unknown Deepgram fields are ignored.

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

1. Create a Supabase project and run `supabase/schema.sql` in the SQL editor.
2. Import this repo as a Vercel project. The Python runtime picks up FastAPI from `pyproject.toml`.
3. In the Vercel project settings, set the same variables as `.env.example`:
   - `XAI_API_KEY`, `XAI_BASE_URL`, `XAI_MODEL`
   - `FREESOUND_API_KEY` and `FREESOUND_BASE_URL` if you are not shipping a filled catalog, plus `FREESOUND_CATALOG_ONLY` (default true) and optional `SFX_CATALOG_PATH`
   - `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_SFX_BUCKET`
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

`tests/test_mixer.py` checks timestamp alignment directly: a clip longer than its cue is audible only inside that window, and the timeline length stays equal to the story.

## Layout

```
app/main.py                 create_app, uvicorn entry
app/config.py               pydantic-settings
app/schemas/deepgram.py     Deepgram JSON -> transcript
app/schemas/sfx.py          cue schema and alignment
app/services/xai.py        xAI Chat Completions client
app/services/freesound.py   catalog match + preview download
app/services/sfx_catalog.py catalog load, match, and builder ranking
assets/sfx_catalog/catalog.json  fixed kids-book sound slots
scripts/build_sfx_catalog.py     one-shot FreeSound fill for those slots
app/services/mixer.py       silence + overlays -> MP3 bytes
app/services/pipeline.py    wires planning, download, and mix
app/services/deepgram.py   prerecorded POST /v1/listen
app/services/store.py       Supabase table + Storage
app/api/routes.py           /stories and /health
supabase/schema.sql         recordings table and story-sfx bucket
fixtures/deepgram_sample.json
```
