# HackGT TED story backend

FastAPI service that turns a grandparent's story into a timed sound-effects track for the teddy bear.

Local setup, environment variables, and the Deepgram → Muse Spark → FreeSound → MP3 flow are documented in this README as the pipeline lands. The API entrypoint is:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn app.main:app --reload
```

`GET /health` returns `{"status": "ok"}`. ffmpeg (with libmp3lame) is required once the mix step is used. Copy `.env.example` and fill in keys locally; do not commit `.env`.
