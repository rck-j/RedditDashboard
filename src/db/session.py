"""Database session utilities for SQLite-backed persistence."""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, create_engine

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_PATH = DATA_DIR / "search_jobs.db"
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)


def get_session() -> Session:
    """Return a new SQLModel session bound to the shared engine."""

    return Session(engine)


__all__ = ["engine", "get_session", "DATABASE_PATH"]
