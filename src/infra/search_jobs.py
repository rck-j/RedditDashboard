"""Simple SQLite-backed storage for queued Reddit searches."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List

from pydantic import BaseModel, Field

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "search_jobs.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS search_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rq_job_id TEXT,
                status TEXT NOT NULL,
                query TEXT NOT NULL,
                subreddits TEXT NOT NULL,
                time_filter TEXT NOT NULL,
                limit_value INTEGER NOT NULL,
                comments_limit INTEGER NOT NULL,
                result_path TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


_init_db()


class SearchJob(BaseModel):
    """Metadata stored for each queued Reddit search."""

    id: int
    rq_job_id: str | None = None
    status: str
    query: str
    subreddits: List[str] = Field(default_factory=list)
    time_filter: str
    limit: int
    comments_limit: int
    result_path: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str


def _row_to_job(row: sqlite3.Row) -> SearchJob:
    payload = dict(row)
    payload["subreddits"] = json.loads(payload["subreddits"])
    payload["limit"] = payload.pop("limit_value")
    return SearchJob.model_validate(payload)


def create_search_job(
    *,
    query: str,
    subreddits: Iterable[str],
    time_filter: str,
    limit: int,
    comments_limit: int,
) -> SearchJob:
    now = datetime.now(timezone.utc).isoformat()
    subreddits_json = json.dumps(list(subreddits))
    with _get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO search_jobs (
                rq_job_id, status, query, subreddits, time_filter, limit_value,
                comments_limit, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                None,
                "queued",
                query,
                subreddits_json,
                time_filter,
                limit,
                comments_limit,
                now,
                now,
            ),
        )
        job_id = cursor.lastrowid
        row = conn.execute(
            "SELECT * FROM search_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    if row is None:  # pragma: no cover - defensive
        raise RuntimeError("Failed to create search job")
    return _row_to_job(row)


def set_rq_job_id(job_id: int, rq_job_id: str) -> SearchJob:
    with _get_connection() as conn:
        conn.execute(
            "UPDATE search_jobs SET rq_job_id = ?, updated_at = ? WHERE id = ?",
            (rq_job_id, datetime.now(timezone.utc).isoformat(), job_id),
        )
        row = conn.execute(
            "SELECT * FROM search_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    if row is None:  # pragma: no cover - defensive
        raise RuntimeError(f"Unknown search job {job_id}")
    return _row_to_job(row)


def update_status(
    job_id: int,
    *,
    status: str,
    result_path: str | None = None,
    error: str | None = None,
) -> SearchJob:
    with _get_connection() as conn:
        conn.execute(
            """
            UPDATE search_jobs
            SET status = ?, result_path = COALESCE(?, result_path),
                error = COALESCE(?, error), updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                result_path,
                error,
                datetime.now(timezone.utc).isoformat(),
                job_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM search_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    if row is None:  # pragma: no cover - defensive
        raise RuntimeError(f"Unknown search job {job_id}")
    return _row_to_job(row)


def mark_running(job_id: int) -> SearchJob:
    return update_status(job_id, status="running")


def mark_succeeded(job_id: int, *, result_path: str) -> SearchJob:
    return update_status(job_id, status="succeeded", result_path=result_path)


def mark_failed(job_id: int, *, error: str) -> SearchJob:
    return update_status(job_id, status="failed", error=error)


__all__ = [
    "SearchJob",
    "create_search_job",
    "set_rq_job_id",
    "mark_running",
    "mark_succeeded",
    "mark_failed",
]
