"""Helpers for interacting with SearchJob ORM objects."""

from __future__ import annotations

from typing import Iterable, List, Sequence

import logging
from sqlalchemy import func
from sqlmodel import select

from src.db.models import JobStatus, SearchJob
from src.db.session import get_session
from src.infra.observability import log_structured


logger = logging.getLogger(__name__)


def create_search_job(
    *,
    user_id: int,
    query: str,
    subreddits: Iterable[str],
    time_filter: str,
    limit: int,
    comments_limit: int,
) -> SearchJob:
    """Persist a new SearchJob row and return it."""

    job = SearchJob(
        user_id=user_id,
        query=query,
        subreddits=list(subreddits),
        time_filter=time_filter,
        limit=limit,
        comments_limit=comments_limit,
    )
    with get_session() as session:
        session.add(job)
        session.commit()
        session.refresh(job)
    log_structured(
        logger,
        logging.INFO,
        "job_submitted",
        job_id=job.id,
        user_id=user_id,
        query=query,
        subreddits=list(subreddits),
        time_filter=time_filter,
        limit=limit,
        comments_limit=comments_limit,
        created_at=job.created_at,
    )
    return job


def get_search_job(job_id: int) -> SearchJob:
    """Fetch a SearchJob by primary key."""

    with get_session() as session:
        job = session.get(SearchJob, job_id)
        if job is None:  # pragma: no cover - defensive
            raise RuntimeError(f"Unknown search job {job_id}")
        session.refresh(job)
        return job


def list_search_jobs() -> List[SearchJob]:
    """Return all recorded jobs sorted by creation time."""

    with get_session() as session:
        return list(
            session.exec(
                select(SearchJob).order_by(SearchJob.created_at.desc())
            )
        )


def count_active_jobs(
    statuses: Sequence[JobStatus] | None = None,
    *,
    user_id: int | None = None,
) -> int:
    """Return the number of jobs currently queued or running.

    When ``user_id`` is provided, the count is scoped to that owner; otherwise
    it reflects the global total.
    """

    active_statuses = tuple(statuses or (JobStatus.QUEUED, JobStatus.RUNNING))
    with get_session() as session:
        stmt = select(func.count()).where(SearchJob.is_deleted.is_(False))
        if active_statuses:
            stmt = stmt.where(SearchJob.status.in_(active_statuses))
        if user_id is not None:
            stmt = stmt.where(SearchJob.user_id == user_id)
        total = session.exec(stmt).one()
        return int(total or 0)


def count_jobs_created_since(*, user_id: int, since_utc) -> int:
    """Return the number of jobs a user has created since ``since_utc``."""

    with get_session() as session:
        stmt = select(func.count()).where(
            SearchJob.is_deleted.is_(False),
            SearchJob.user_id == user_id,
            SearchJob.created_at >= since_utc,
        )
        total = session.exec(stmt).one()
        return int(total or 0)


__all__ = [
    "SearchJob",
    "count_active_jobs",
    "count_jobs_created_since",
    "create_search_job",
    "get_search_job",
    "list_search_jobs",
]
