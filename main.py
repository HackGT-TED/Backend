"""Uvicorn entry: ``uvicorn main:app`` and ``uvicorn app.main:app`` are the same app."""

from app.main import app

__all__ = ["app"]
