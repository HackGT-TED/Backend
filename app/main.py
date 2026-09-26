"""Uvicorn entrypoint: ``uvicorn app.main:app``."""

from fastapi import FastAPI

from app import __version__
from app.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API. Later commits attach the story pipeline to this app."""

    settings = settings or get_settings()
    app = FastAPI(
        title="HackGT TED Story Backend",
        version=__version__,
        summary="Turn grandparent story transcripts into a timed sound-effects track.",
    )
    app.state.settings = settings

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
