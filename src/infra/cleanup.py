"""Utilities for trimming persisted search jobs and reports."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Collection, Dict, Mapping

from sqlmodel import delete, select

from src.db.models import SearchJob, SubscriptionPlan, User
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


def purge_expired_jobs_by_plan(
    retention_days_by_plan: Mapping[SubscriptionPlan, int],
    *,
    archive_only_plans: Collection[SubscriptionPlan] | None = None,
) -> Dict[SubscriptionPlan, int]:
    """Apply per-plan retention policies.

    Jobs owned by users on plans listed in ``archive_only_plans`` are marked as
    deleted when they exceed their retention window; others are purged to free
    storage. Returns the number of jobs archived/purged per plan.
    """

    archive_plans = set(archive_only_plans or [])
    now = datetime.now(timezone.utc)
    removed_counts: Dict[SubscriptionPlan, int] = {}

    with get_session() as session:
        for plan, retention_days in retention_days_by_plan.items():
            if retention_days <= 0:
                continue
            cutoff = now - timedelta(days=retention_days)
            stale_jobs = list(
                session.exec(
                    select(SearchJob)
                    .join(User)
                    .where(
                        User.subscription_plan == plan,
                        SearchJob.created_at < cutoff,
                    )
                )
            )
            if not stale_jobs:
                continue

            if plan in archive_plans:
                for job in stale_jobs:
                    job.is_deleted = True
                    session.add(job)
                session.commit()
            else:
                session.exec(
                    delete(SearchJob).where(
                        SearchJob.id.in_([job.id for job in stale_jobs])
                    )
                )
                session.commit()
            removed_counts[plan] = len(stale_jobs)

    return removed_counts


__all__ = ["purge_expired_jobs", "purge_expired_jobs_by_plan"]
