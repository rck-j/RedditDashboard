"""Report schema normalization helpers."""
from __future__ import annotations

from datetime import datetime
from typing import List, Tuple

from pydantic import BaseModel, Field
from sqlmodel import select

from src.db.models import JobStatus, PersistedPostReport, SearchJob
from src.db.session import get_session

try:
    from red import PostReport as BasePostReport
except ImportError:  # pragma: no cover - fallback for standalone execution
    class InitialAssessment(BaseModel):
        """Structured output for the initial title-only screen."""

        is_automation: bool = Field(...)
        rationale: str = Field(...)

    class AutomationInsight(BaseModel):
        """Structured output for the full deep-dive analysis."""

        automation_summary: str
        deep_analysis: str
        automation_complexity: str
        required_tools: List[str] = Field(default_factory=list)

    class BasePostReport(BaseModel):
        submission_id: str
        subreddit: str
        title: str
        url: str
        permalink: str
        created: str
        created_utc: datetime
        score: int
        num_comments: int
        initial_assessment: InitialAssessment
        automation_insight: AutomationInsight
else:  # pragma: no cover - red.py already depends on src.services definitions
    from src.services import AutomationInsight, InitialAssessment


class PostReport(BasePostReport):
    """Local alias to ensure consistent validation."""


def _summarize_text(value: str, *, limit: int = 240) -> str:
    """Collapse whitespace and clamp long summaries."""

    collapsed = " ".join((value or "").split()).strip()
    if not collapsed:
        return "Summary unavailable."
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit].rstrip()}…"


def _legacy_report_from(report: PersistedPostReport) -> PostReport:
    """Adapt a stored report row to the original PostReport schema."""

    deep_analysis = report.insight_text or "Detailed analysis pending."
    complexity = (report.automation_complexity or "unknown").strip() or "unknown"
    summary = _summarize_text(deep_analysis)
    is_automation = complexity.lower() not in {"unknown", "n/a"}
    initial_assessment = InitialAssessment(
        is_automation=is_automation,
        rationale=summary,
    )
    automation_insight = AutomationInsight(
        automation_summary=summary,
        deep_analysis=deep_analysis,
        automation_complexity=complexity,
        required_tools=list(report.required_tools or []),
    )
    created_utc = getattr(report, "created_utc", None)
    if not isinstance(created_utc, datetime):
        created_utc = report.created_at
    return PostReport(
        submission_id=report.submission_id,
        subreddit=report.subreddit,
        title=report.title,
        url=report.url,
        permalink=report.permalink,
        created=report.created,
        created_utc=created_utc,
        score=report.score,
        num_comments=report.num_comments,
        initial_assessment=initial_assessment,
        automation_insight=automation_insight,
    )


def _latest_succeeded_job(session) -> SearchJob | None:
    stmt = (
        select(SearchJob)
        .where(SearchJob.status == JobStatus.SUCCEEDED)
        .where(SearchJob.is_deleted.is_(False))
        .order_by(SearchJob.finished_at.desc(), SearchJob.created_at.desc())
        .limit(1)
    )
    return session.exec(stmt).first()


def _latest_reports() -> tuple[SearchJob | None, List[PostReport]]:
    with get_session() as session:
        job = _latest_succeeded_job(session)
        if job is None:
            return None, []
        persisted_reports = _fetch_reports(session, job.id)
    return job, [_legacy_report_from(report) for report in persisted_reports]


def _fetch_reports(session, job_id: int) -> List[PersistedPostReport]:
    return list(
        session.exec(
            select(PersistedPostReport)
            .where(PersistedPostReport.search_job_id == job_id)
            .order_by(PersistedPostReport.created_at.desc())
        )
    )


def get_reports(return_job: bool = False) -> Tuple[SearchJob | None, List[PostReport]] | List[PostReport]:
    """Return the latest completed search as legacy PostReports."""

    job, reports = _latest_reports()
    if return_job:
        return job, reports
    return reports


__all__ = ["PostReport", "get_reports"]
