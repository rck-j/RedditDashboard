"""Utility helpers for aggregating persisted automation insights."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Iterable, Literal, Sequence

from sqlalchemy import func
from sqlmodel import Session, select

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
class TopSubredditCount:
    """Top subreddit counts for a search job."""

    subreddit: str
    count: int


@dataclass(frozen=True)
class KeywordFrequency:
    """Keyword frequency entry calculated from persisted reports."""

    keyword: str
    count: int


@dataclass(frozen=True)
class ToolFrequency:
    """Frequency map entry for automation tools required across reports."""

    label: str
    count: int


@dataclass(frozen=True)
class SummaryMetrics:
    """Snapshot of aggregated metrics for a collection of reports."""

    total_posts: int
    automation_percentage: float | None
    average_score: float | None
    average_comment_count: float | None


@dataclass(frozen=True)
class TimelineBucket:
    """Counts of total and automation posts grouped by time bucket."""

    bucket_start: datetime
    bucket_end: datetime
    total_posts: int
    automation_posts: int


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


def build_timeline(
    reports: Sequence[PersistedPostReport],
    *,
    bucket_size: Literal["daily", "weekly"] = "daily",
) -> list[TimelineBucket]:
    """Group reports into chronological buckets for charting."""

    if bucket_size not in {"daily", "weekly"}:
        raise ValueError("bucket_size must be 'daily' or 'weekly'.")

    bucket_delta = timedelta(days=1 if bucket_size == "daily" else 7)
    buckets: dict[datetime, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "automation": 0}
    )

    for report in reports:
        timestamp = _extract_created_utc(report)
        if timestamp is None:
            continue
        bucket_start = _coerce_bucket_start(timestamp, bucket_size)
        entry = buckets[bucket_start]
        entry["total"] += 1
        if _is_automation_candidate(report):
            entry["automation"] += 1

    ordered = sorted(buckets.items(), key=lambda item: item[0])
    return [
        TimelineBucket(
            bucket_start=start,
            bucket_end=start + bucket_delta,
            total_posts=counts["total"],
            automation_posts=counts["automation"],
        )
        for start, counts in ordered
    ]


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


def fetch_top_subreddits(
    session: Session,
    *,
    job_id: int,
    limit: int = 5,
) -> list[TopSubredditCount]:
    """Return the most common subreddits for a search job.

    Results are sorted by descending count, then alphabetically for stability.
    """

    stmt = (
        select(
            PersistedPostReport.subreddit,
            func.count(PersistedPostReport.id).label("subreddit_count"),
        )
        .where(PersistedPostReport.search_job_id == job_id)
        .group_by(PersistedPostReport.subreddit)
        .order_by(
            func.count(PersistedPostReport.id).desc(),
            PersistedPostReport.subreddit.asc(),
        )
        .limit(limit)
    )
    rows = session.exec(stmt).all()
    return [TopSubredditCount(subreddit=row[0], count=int(row[1] or 0)) for row in rows]


def extract_top_keywords(
    reports: Sequence[PersistedPostReport],
    *,
    limit: int = 10,
) -> list[KeywordFrequency]:
    """Return the most frequent keywords derived from persisted reports."""

    if not reports:
        return []

    counter: Counter[str] = Counter()
    for report in reports:
        counter.update(_tokenize(report.title))
        counter.update(_tokenize(getattr(report, "insight_text", "")))

    most_common = counter.most_common()
    most_common.sort(key=lambda entry: (-entry[1], entry[0]))
    trimmed = most_common[:limit]
    return [KeywordFrequency(keyword=word, count=count) for word, count in trimmed]


def calculate_tool_frequencies(
    reports: Sequence[PersistedPostReport],
) -> list[ToolFrequency]:
    """Return normalized tool usage counts for a collection of reports."""

    if not reports:
        return []

    counter: Counter[str] = Counter()
    for report in reports:
        for raw_tool in getattr(report, "required_tools", []) or []:
            if raw_tool is None:
                continue
            parts = TOOL_SPLIT_PATTERN.split(raw_tool)
            if not parts:
                parts = [raw_tool]
            for part in parts:
                normalized = (part or "").strip().lower()
                if not normalized:
                    continue
                counter[normalized] += 1

    entries = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return [ToolFrequency(label=label, count=count) for label, count in entries]


def _is_automation_candidate(report: PersistedPostReport) -> bool:
    complexity = _normalized_complexity(report.automation_complexity)
    return bool(complexity) and complexity not in {"unknown", "n/a"}


def _normalized_complexity(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    return normalized or "unknown"


def _tokenize(text: str | None) -> Iterable[str]:
    if not text:
        return []
    tokens: list[str] = []
    for match in TOKEN_PATTERN.findall(text.lower()):
        token = match.strip("'")
        if not token or token in STOP_WORDS or token.isdigit() or len(token) < 3:
            continue
        tokens.append(token)
    return tokens


def _extract_created_utc(
    report: PersistedPostReport,
) -> datetime | None:
    timestamp = getattr(report, "created_utc", None)
    if not isinstance(timestamp, datetime):
        timestamp = getattr(report, "created_at", None)
    if timestamp is None:
        return None
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _coerce_bucket_start(timestamp: datetime, bucket_size: str) -> datetime:
    normalized = timestamp.astimezone(timezone.utc)
    if bucket_size == "weekly":
        normalized -= timedelta(days=normalized.weekday())
    return normalized.replace(hour=0, minute=0, second=0, microsecond=0)


__all__ = [
    "AUTOMATION_COMPLEXITY_LEVELS",
    "ComplexityDistribution",
    "ComplexityTimelineBucket",
    "KeywordFrequency",
    "SummaryMetrics",
    "TimelineBucket",
    "TopSubredditCount",
    "ToolFrequency",
    "build_timeline",
    "calculate_average_comment_count",
    "calculate_average_score",
    "calculate_complexity_distribution",
    "calculate_automation_percentage",
    "calculate_total_posts",
    "calculate_tool_frequencies",
    "extract_top_keywords",
    "fetch_top_subreddits",
    "summarize_reports",
]
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9']+")
TOOL_SPLIT_PATTERN = re.compile(r"(?:/|,|\+|&|\band\b)", re.IGNORECASE)
STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "about",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "can",
        "for",
        "from",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "our",
        "that",
        "the",
        "their",
        "this",
        "to",
        "with",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "you",
        "your",
    }
)
