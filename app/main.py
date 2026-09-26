"""Uvicorn entrypoint: ``uvicorn app.main:app``.

Vercel loads the same ``app`` object. See ``[tool.vercel]`` in ``pyproject.toml``.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import router
from app.config import Settings, get_settings
from app.services.deepgram import DeepgramClient, HttpDeepgramClient
from app.services.freesound import FreeSoundClient, HttpFreeSoundClient
from app.services.store import RecordingStore, SupabaseRecordingStore
from app.services.xai import HttpXaiClient, XaiClient


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    for name in ("xai", "freesound", "deepgram"):
        client = getattr(app.state, name, None)
        close = getattr(client, "close", None)
        if callable(close):
            close()


def create_app(
    settings: Settings | None = None,
    xai_client: XaiClient | None = None,
    freesound_client: FreeSoundClient | None = None,
    store: RecordingStore | None = None,
    deepgram_client: DeepgramClient | None = None,
) -> FastAPI:
    """Build the API. Persistence is a Supabase client unless a test store is passed."""

    settings = settings or get_settings()
    app = FastAPI(
        title="HackGT TED Story Backend",
        version=__version__,
        summary="Turn a story recording into the same audio with catalog sound effects lined up on it.",
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.store = store if store is not None else SupabaseRecordingStore(settings)
    app.state.xai = xai_client or HttpXaiClient(settings)
    app.state.freesound = freesound_client or HttpFreeSoundClient(settings)
    app.state.deepgram = deepgram_client or HttpDeepgramClient(settings)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
