"""Engine and session factory. Tables are created with ``create_all``."""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base


def create_session_factory(database_url: str) -> sessionmaker[Session]:
    connect_args: dict = {}
    engine_kwargs: dict = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        _ensure_sqlite_parent(database_url)
        if database_url in {"sqlite://", "sqlite:///:memory:"}:
            engine_kwargs["poolclass"] = StaticPool
    engine = create_engine(database_url, connect_args=connect_args, **engine_kwargs)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _ensure_sqlite_parent(database_url: str) -> None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return
    path = database_url[len(prefix) :]
    if not path or path == ":memory:":
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
