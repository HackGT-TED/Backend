"""Uvicorn entrypoint: ``uvicorn app.main:app``."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import router
from app.config import Settings, get_settings
from app.db.session import create_session_factory
from app.services.freesound import FreeSoundClient, HttpFreeSoundClient
from app.services.muse_spark import HttpMuseSparkClient, MuseSparkClient


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    for name in ("muse", "freesound"):
        client = getattr(app.state, name, None)
        close = getattr(client, "close", None)
        if callable(close):
            close()


def create_app(
    settings: Settings | None = None,
    muse_client: MuseSparkClient | None = None,
    freesound_client: FreeSoundClient | None = None,
) -> FastAPI:
    """Build the API with its database, external clients, and story routes."""

    settings = settings or get_settings()
    app = FastAPI(
        title="HackGT TED Story Backend",
        version=__version__,
        summary="Turn grandparent story transcripts into a timed sound-effects track.",
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.session_factory = create_session_factory(settings.database_url)
    app.state.muse = muse_client or HttpMuseSparkClient(settings)
    app.state.freesound = freesound_client or HttpFreeSoundClient(settings)

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
