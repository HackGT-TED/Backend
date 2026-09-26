# HackGT TED story backend

Grandparents record a story for a child. Deepgram turns that recording into JSON with word and segment timestamps. This service asks xAI where the sound effects should go, downloads matching clips from FreeSound, and mixes an **SFX-only MP3** aligned to those timestamps. The grandparents' web app and the kids' app load the stored recording and the MP3.

v1 does not call Deepgram and does not upload the original voice recording. Clients send the Deepgram JSON they already have. Processing is synchronous: `POST /stories/process` finishes the mix before it responds.

```
Deepgram JSON
  -> transcript text + word/segment timestamps
  -> xAI chat completions (structured SFX cues)
  -> FreeSound text search + preview MP3 per cue
  -> silent timeline with clips overlaid at those timestamps
  -> SQLite row + GET /stories/{id}/sfx
```

## Requirements

- Python 3.11 or 3.12 (3.13 is supported via the `audioop-lts` dependency declared in `pyproject.toml`)
- [ffmpeg](https://ffmpeg.org/) on `PATH`, built with libmp3lame. pydub uses it to decode FreeSound MP3 previews and to write the SFX file.

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

Edit `.env` and set `XAI_API_KEY` and `FREESOUND_API_KEY`. Do not commit `.env`. The sample file has empty values only.

```bash
uvicorn app.main:app --reload
```

The API listens on `http://127.0.0.1:8000`. Interactive docs are at `/docs`. `GET /health` returns `{"status": "ok"}` with no keys configured. `POST /stories/process` returns **503** until the keys the pipeline needs are set.

Tables are created with SQLAlchemy `create_all` on startup. The default database is `sqlite:///./data/hackgt.db`. There is no Alembic history; delete that file if you change the models locally. Mixed MP3s are written under `MEDIA_DIR` (default `./media/{recording_id}/sfx.mp3`).

## Environment

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `XAI_API_KEY` | to process stories | empty | Bearer token for xAI Chat Completions. |
| `XAI_BASE_URL` | no | `https://api.x.ai/v1` | OpenAI-compatible base URL. |
| `XAI_MODEL` | no | `grok-4.7` | Chat model id. `grok-4.7` supports structured outputs. |
| `FREESOUND_API_KEY` | to download SFX | empty | FreeSound APIv2 token. |
| `FREESOUND_BASE_URL` | no | `https://freesound.org` | API host. |
| `DATABASE_URL` | no | `sqlite:///./data/hackgt.db` | SQLAlchemy URL. |
| `DEEPGRAM_API_KEY` | no | empty | Reserved. v1 does not call Deepgram. |
| `DEEPGRAM_MODEL` | no | `nova-2` | Reserved for a later direct-transcription path. |
| `DEEPGRAM_LANGUAGE` | no | `en` | Reserved. |
| `MEDIA_DIR` | no | `./media` | Where SFX MP3s are written. |
| `CORS_ORIGINS` | no | `*` | Comma-separated browser origins for the web and kids apps. |
| `HTTP_TIMEOUT_SECONDS` | no | `60` | Timeout for xAI and FreeSound. Raise this if `grok-4.7` reasoning runs long. |

A missing `XAI_API_KEY` or `FREESOUND_API_KEY` raises before any request is sent, and the API returns **503** with the recording id of the failed job.

## API

### `POST /stories/process`

Body: Deepgram JSON under `deepgram`, plus optional `story_id`, `title`, `narrator`, and `source_audio_url`. A raw Deepgram document (top-level `results` or `transcript`) is accepted too.

```bash
jq -n --slurpfile dg fixtures/deepgram_sample.json \
  '{title:"Rain story", story_id:"demo-1", narrator:"Grandma", deepgram:$dg[0]}' \
  | curl -sS -X POST http://127.0.0.1:8000/stories/process \
      -H 'Content-Type: application/json' -d @-
```

`201` response includes `id`, `status` (`ready`), cue timestamps, `warnings`, and `sfx_url`.

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

The SFX-only MP3 (`audio/mpeg`). This file is silence plus the placed effects. It does not contain the grandparent's voice.

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

## FreeSound

- Search: `GET {FREESOUND_BASE_URL}/apiv2/search/text/?query=...&fields=id,name,previews,duration&page_size=5&sort=rating_desc`
- Auth: `Authorization: Token $FREESOUND_API_KEY` on the API host only
- Audio: `previews.preview-hq-mp3` (falls back to `preview-lq-mp3`)

Preview MP3s are what this service mixes. Original-quality downloads need OAuth2, which v1 does not implement. HTTP 429 is retried up to three times using `Retry-After` or a short backoff. If every cue is still rate-limited, the request returns **429**. Create a token at <https://freesound.org/apiv2/apply>.

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
app/services/freesound.py   search + preview download
app/services/mixer.py       silence + overlays -> MP3
app/services/pipeline.py    wires the three steps
app/db/models.py            Recording, StoryAudio
app/api/routes.py           /stories and /health
fixtures/deepgram_sample.json
```
