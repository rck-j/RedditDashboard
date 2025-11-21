"""Subscription plan policies and limits.

This module centralizes per-plan quotas so the API can enforce
daily/concurrent limits, retention windows, and archival behavior
consistently across endpoints and background tasks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from src.db.models import SubscriptionPlan


@dataclass(frozen=True)
class PlanPolicy:
    """Configuration for a subscription plan."""

    daily_job_limit: int | None
    concurrent_job_limit: int | None
    retention_days: int
    archive_instead_of_delete: bool = False


PLAN_POLICIES: Dict[SubscriptionPlan, PlanPolicy] = {
    SubscriptionPlan.FREE: PlanPolicy(
        daily_job_limit=5,
        concurrent_job_limit=1,
        retention_days=7,
    ),
    SubscriptionPlan.PRO: PlanPolicy(
        daily_job_limit=25,
        concurrent_job_limit=3,
        retention_days=30,
    ),
    SubscriptionPlan.ENTERPRISE: PlanPolicy(
        daily_job_limit=100,
        concurrent_job_limit=10,
        retention_days=90,
        archive_instead_of_delete=True,
    ),
}


def get_plan_policy(plan: SubscriptionPlan) -> PlanPolicy:
    """Return the policy for the given plan, falling back to FREE settings."""

    return PLAN_POLICIES.get(plan, PLAN_POLICIES[SubscriptionPlan.FREE])


def plan_retention_days() -> Dict[SubscriptionPlan, int]:
    """Expose retention periods keyed by plan for cleanup tasks."""

    return {
        plan: policy.retention_days for plan, policy in PLAN_POLICIES.items()
    }


def archival_plans() -> set[SubscriptionPlan]:
    """Return plans whose jobs should be archived instead of deleted."""

    return {
        plan
        for plan, policy in PLAN_POLICIES.items()
        if policy.archive_instead_of_delete
    }


__all__ = [
    "PlanPolicy",
    "PLAN_POLICIES",
    "archival_plans",
    "get_plan_policy",
    "plan_retention_days",
]
