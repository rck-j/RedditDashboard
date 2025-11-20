"""Database session utilities for Postgres-backed persistence."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Tuple
from warnings import warn

from sqlalchemy.engine import Engine
from sqlalchemy.exc import NoSuchModuleError
from sqlmodel import Session, create_engine

_DEFAULT_POSTGRES_URL = "postgresql+psycopg://reddash:reddash@localhost:5432/reddash"
_REQUESTED_DATABASE_URL = os.getenv("DATABASE_URL", _DEFAULT_POSTGRES_URL)
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_SQLITE_FALLBACK_URL = f"sqlite:///{_DATA_DIR / 'search_jobs.db'}"


def _engine_kwargs(url: str) -> Dict[str, Any]:
    if url.startswith("sqlite"):
        return {"echo": False, "connect_args": {"check_same_thread": False}}
    return {"echo": False, "pool_pre_ping": True}


def _build_engine(url: str) -> Tuple[Engine, str]:
    try:
        return create_engine(url, **_engine_kwargs(url)), url
    except NoSuchModuleError:
        if url.startswith("postgresql"):
            warn(
                "psycopg driver missing; falling back to SQLite. "
                "Install psycopg[binary] and set DATABASE_URL to your Postgres instance "
                "for production deployments."
            )
            return create_engine(
                _SQLITE_FALLBACK_URL, **_engine_kwargs(_SQLITE_FALLBACK_URL)
            ), _SQLITE_FALLBACK_URL
        raise


engine, DATABASE_URL = _build_engine(_REQUESTED_DATABASE_URL)


def get_session() -> Session:
    """Return a new SQLModel session bound to the shared engine."""

    return Session(engine)


__all__ = ["engine", "get_session", "DATABASE_URL"]
