"""Helpers for interacting with SearchJob ORM objects."""

from __future__ import annotations

from typing import Iterable, List, Sequence

from sqlalchemy import func
from sqlmodel import select

from src.db.models import JobStatus, SearchJob
from src.db.session import get_session


def create_search_job(
    *,
    query: str,
    subreddits: Iterable[str],
    time_filter: str,
    limit: int,
    comments_limit: int,
) -> SearchJob:
    """Persist a new SearchJob row and return it."""

    job = SearchJob(
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
) -> int:
    """Return the number of jobs currently queued or running."""

    active_statuses = tuple(statuses or (JobStatus.QUEUED, JobStatus.RUNNING))
    with get_session() as session:
        stmt = select(func.count()).where(SearchJob.is_deleted.is_(False))
        if active_statuses:
            stmt = stmt.where(SearchJob.status.in_(active_statuses))
        total = session.exec(stmt).one()
        return int(total or 0)


__all__ = [
    "SearchJob",
    "count_active_jobs",
    "create_search_job",
    "get_search_job",
    "list_search_jobs",
]
