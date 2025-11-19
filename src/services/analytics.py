"""Utility helpers for aggregating persisted automation insights."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src.db.models import PersistedPostReport


@dataclass(frozen=True)
class SummaryMetrics:
    """Snapshot of aggregated metrics for a collection of reports."""

    total_posts: int
    automation_percentage: float | None
    average_score: float | None
    average_comment_count: float | None


def calculate_total_posts(reports: Sequence[PersistedPostReport]) -> int:
    """Return the total number of persisted reports."""

    return len(reports)


def calculate_automation_percentage(
    reports: Sequence[PersistedPostReport],
) -> float | None:
    """Return the percentage of reports flagged as automation opportunities."""

    total = len(reports)
    if total == 0:
        return None
    automated_count = sum(1 for report in reports if _is_automation_candidate(report))
    return (automated_count / total) * 100


def calculate_average_score(reports: Sequence[PersistedPostReport]) -> float | None:
    """Return the average score across reports or ``None`` when empty."""

    total = len(reports)
    if total == 0:
        return None
    return sum(report.score for report in reports) / total


def calculate_average_comment_count(
    reports: Sequence[PersistedPostReport],
) -> float | None:
    """Return the average number of comments across reports."""

    total = len(reports)
    if total == 0:
        return None
    return sum(report.num_comments for report in reports) / total


def summarize_reports(reports: Sequence[PersistedPostReport]) -> SummaryMetrics:
    """Return a ``SummaryMetrics`` snapshot for the given reports."""

    return SummaryMetrics(
        total_posts=calculate_total_posts(reports),
        automation_percentage=calculate_automation_percentage(reports),
        average_score=calculate_average_score(reports),
        average_comment_count=calculate_average_comment_count(reports),
    )


def _is_automation_candidate(report: PersistedPostReport) -> bool:
    complexity = (report.automation_complexity or "").strip().lower()
    return bool(complexity) and complexity not in {"unknown", "n/a"}


__all__ = [
    "SummaryMetrics",
    "calculate_average_comment_count",
    "calculate_average_score",
    "calculate_automation_percentage",
    "calculate_total_posts",
    "summarize_reports",
]
