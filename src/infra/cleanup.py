"""Utilities for trimming persisted search jobs and reports."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from sqlmodel import delete, select

from src.db.models import SearchJob
from src.db.session import get_session

DEFAULT_JOB_TTL_DAYS = 30


def _ttl_days() -> int:
    raw_value = os.getenv("SEARCH_JOB_TTL_DAYS")
    try:
        ttl = int(raw_value) if raw_value is not None else DEFAULT_JOB_TTL_DAYS
    except ValueError:
        ttl = DEFAULT_JOB_TTL_DAYS
    return max(ttl, 0)


def purge_expired_jobs(*, max_age_days: int | None = None) -> int:
    """Delete jobs older than ``max_age_days`` and return the count removed."""

    ttl_days = max_age_days if max_age_days is not None else _ttl_days()
    if ttl_days == 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    with get_session() as session:
        stale_job_ids = list(
            session.exec(
                select(SearchJob.id).where(SearchJob.created_at < cutoff)
            )
        )
        if not stale_job_ids:
            return 0

        session.exec(delete(SearchJob).where(SearchJob.id.in_(stale_job_ids)))
        session.commit()
        return len(stale_job_ids)


__all__ = ["purge_expired_jobs"]
