"""Helpers for generating cached SearchJob stats payloads."""

from __future__ import annotations

from typing import Any, Sequence

from sqlmodel import Session, select

from src.db.models import JobStatus, PersistedPostReport, SearchJob
from src.services.analytics import (
    build_timeline,
    calculate_complexity_distribution,
    calculate_tool_frequencies,
    extract_top_keywords,
    fetch_top_subreddits,
    summarize_reports,
)


def build_search_job_stats(
    session: Session,
    job: SearchJob,
    *,
    reports: Sequence[PersistedPostReport] | None = None,
) -> dict[str, Any]:
    """Return a serialized stats snapshot for the provided job."""

    stats: dict[str, Any] = {
        "processed_count": job.processed_count,
        "total_count": job.total_count,
        "average_score": job.average_score,
        "report_count": 0,
        "summary": None,
        "complexity": None,
        "timeline": None,
        "subreddits": None,
        "keywords": None,
        "tools": None,
    }

    if job.status != JobStatus.SUCCEEDED:
        return stats

    resolved_reports = list(
        reports
        if reports is not None
        else session.exec(
            select(PersistedPostReport).where(
                PersistedPostReport.search_job_id == job.id
            )
        ).all()
    )

    stats["report_count"] = len(resolved_reports)

    summary_metrics = summarize_reports(resolved_reports)
    stats["summary"] = {
        "total_posts": summary_metrics.total_posts,
        "automation_percentage": summary_metrics.automation_percentage,
        "average_score": summary_metrics.average_score,
        "average_comment_count": summary_metrics.average_comment_count,
    }

    complexity_snapshot = calculate_complexity_distribution(
        resolved_reports, include_timeline=True
    )
    stats["complexity"] = {
        "total": complexity_snapshot.total,
        "counts": complexity_snapshot.counts,
        "percentages": complexity_snapshot.percentages,
        "timeline": (
            [
                {
                    "bucket": bucket.bucket,
                    "automation_count": bucket.automation_count,
                    "non_automation_count": bucket.non_automation_count,
                }
                for bucket in complexity_snapshot.timeline or []
            ]
            or None
        ),
    }

    timeline_buckets = build_timeline(resolved_reports, bucket_size="daily")
    stats["timeline"] = (
        {
            "bucket_size": "daily",
            "buckets": [
                {
                    "bucket_start": bucket.bucket_start.isoformat(),
                    "bucket_end": bucket.bucket_end.isoformat(),
                    "total_posts": bucket.total_posts,
                    "automation_posts": bucket.automation_posts,
                }
                for bucket in timeline_buckets
            ],
        }
        if timeline_buckets
        else None
    )

    top_subreddits = fetch_top_subreddits(session, job_id=job.id)
    stats["subreddits"] = [
        {"subreddit": entry.subreddit, "count": entry.count}
        for entry in top_subreddits
    ]

    keywords = extract_top_keywords(resolved_reports)
    stats["keywords"] = [
        {"keyword": entry.keyword, "count": entry.count} for entry in keywords
    ]

    tool_frequencies = calculate_tool_frequencies(resolved_reports)
    stats["tools"] = [
        {"label": entry.label, "count": entry.count} for entry in tool_frequencies
    ]

    return stats


__all__ = ["build_search_job_stats"]
