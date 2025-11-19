"""Utility helpers for aggregating persisted automation insights."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from src.db.models import PersistedPostReport


AUTOMATION_COMPLEXITY_LEVELS: tuple[str, ...] = ("low", "medium", "high", "unknown")
"""Normalized enum values expected by the dashboard frontend."""


@dataclass(frozen=True)
class ComplexityTimelineBucket:
    """Automation vs. non-automation counts per UTC date bucket."""

    bucket: str
    automation_count: int
    non_automation_count: int


@dataclass(frozen=True)
class ComplexityDistribution:
    """Distribution of automation complexity values for persisted reports."""

    total: int
    counts: dict[str, int]
    percentages: dict[str, float]
    timeline: Sequence[ComplexityTimelineBucket] | None = None


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


def calculate_complexity_distribution(
    reports: Sequence[PersistedPostReport],
    *,
    include_timeline: bool = False,
) -> ComplexityDistribution:
    """Return counts/percentages for automation complexity values.

    Args:
        reports: Persisted reports scoped to a single job.
        include_timeline: When ``True``, aggregate automation vs. non-automation
            counts per UTC date bucket (``YYYY-MM-DD``) when timestamps exist.
    """

    total = len(reports)
    counts: dict[str, int] = {level: 0 for level in AUTOMATION_COMPLEXITY_LEVELS}

    if total == 0:
        return ComplexityDistribution(
            total=0,
            counts=counts,
            percentages={key: 0.0 for key in counts},
            timeline=None,
        )

    timeline_totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {"automation": 0, "non_automation": 0}
    )

    for report in reports:
        complexity = _normalized_complexity(report.automation_complexity)
        counts[complexity] = counts.get(complexity, 0) + 1

        if include_timeline:
            created_at = getattr(report, "created_at", None)
            if isinstance(created_at, datetime):
                bucket = created_at.astimezone(timezone.utc).date().isoformat()
                key = "automation" if _is_automation_candidate(report) else "non_automation"
                timeline_totals[bucket][key] += 1

    total = len(reports)
    percentages = {
        key: (value / total) * 100 if total else 0.0 for key, value in counts.items()
    }
    timeline = (
        [
            ComplexityTimelineBucket(
                bucket=bucket,
                automation_count=totals["automation"],
                non_automation_count=totals["non_automation"],
            )
            for bucket, totals in sorted(timeline_totals.items())
        ]
        if include_timeline and timeline_totals
        else None
    )

    return ComplexityDistribution(
        total=total,
        counts=counts,
        percentages=percentages,
        timeline=timeline,
    )


def _is_automation_candidate(report: PersistedPostReport) -> bool:
    complexity = _normalized_complexity(report.automation_complexity)
    return bool(complexity) and complexity not in {"unknown", "n/a"}


def _normalized_complexity(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    return normalized or "unknown"


__all__ = [
    "AUTOMATION_COMPLEXITY_LEVELS",
    "ComplexityDistribution",
    "ComplexityTimelineBucket",
    "SummaryMetrics",
    "calculate_average_comment_count",
    "calculate_average_score",
    "calculate_complexity_distribution",
    "calculate_automation_percentage",
    "calculate_total_posts",
    "summarize_reports",
]
