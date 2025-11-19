"""RQ job for running a Reddit automation search."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import re
from typing import List, Sequence

from dotenv import load_dotenv

from red import PROMPTS, build_openai_client, build_reddit_client
from src.db.models import JobStatus, PersistedPostReport, SearchJob
from src.db.session import get_session
from src.infra.cleanup import purge_expired_jobs
from src.infra.observability import TelemetryRecorder
from src.jobs.job_errors import JobError, job_error_from_exception
from src.services import AnalyzerDependencies, AutomationAnalyzer, PostReport, search_posts
from src.services.job_stats import build_search_job_stats

logger = logging.getLogger(__name__)

load_dotenv()

def run(
    *,
    search_job_id: int,
    subreddits: Sequence[str],
    query: str,
    time_filter: str,
    limit: int,
    comments_limit: int,
) -> str:
    """Execute a Reddit search and persist the resulting analysis report."""

    purge_expired_jobs()
    session = get_session()
    job = session.get(SearchJob, search_job_id)
    if job is None:  # pragma: no cover - defensive
        session.close()
        raise RuntimeError(f"Unknown search job {search_job_id}")

    job.status = JobStatus.RUNNING
    job.started_at = datetime.now(timezone.utc)
    job.processed_count = 0
    job.total_count = 0
    job.error_message = None
    job.error_detail = None
    job.average_score = None
    _commit_job(session, job)

    reddit_client = build_reddit_client()
    openai_client, model_name = build_openai_client()
    telemetry = TelemetryRecorder(logger=logger, job_id=job.id)
    telemetry.job_started(queued_at=job.created_at)
    deps = AnalyzerDependencies(
        reddit=reddit_client,
        openai=openai_client,
        openai_model=model_name,
        prompts=PROMPTS,
        telemetry=telemetry,
    )
    analyzer = AutomationAnalyzer(deps)
    batch: List[PersistedPostReport] = []
    processed = 0
    score_total = 0
    job_error: JobError | None = None
    try:
        for summary in search_posts(
            reddit_client,
            subreddits=subreddits,
            query=query,
            time_filter=time_filter,
            limit=limit,
            telemetry=telemetry,
        ):
            job.total_count += 1
            _commit_job(session, job)
            report = analyzer.analyze_post(summary, comment_limit=comments_limit)
            batch.append(_persisted_report_from(report, job.id))
            processed += 1
            job.processed_count = processed
            score_total += report.score
            job.average_score = score_total / processed
            _commit_job(session, job)
            telemetry.record_post_processed(processed)
            if processed % 10 == 0:
                telemetry.job_progress(
                    processed=processed,
                    total_seen=job.total_count,
                )
            _flush_batch(session, job, batch)

        _flush_batch(session, job, batch, force=True)

        job.status = JobStatus.SUCCEEDED
        job.finished_at = datetime.now(timezone.utc)
        job.stats = build_search_job_stats(session, job)
        _commit_job(session, job)
        telemetry.job_progress(processed=processed, total_seen=job.total_count)
        telemetry.job_completed(status="succeeded")
        return str(job.id)
    except Exception as exc:  # pragma: no cover - depends on live APIs
        job_error = job_error_from_exception(exc)
        logger.exception(
            "Search job %s failed (%s): %s",
            job.id if job else search_job_id,
            job_error.code,
            job_error.message,
        )
        try:
            _flush_batch(session, job, batch, force=True)
        except Exception:  # pragma: no cover - defensive logging
            logger.exception(
                "Unable to flush buffered post reports before failing job %s",
                job.id if job else search_job_id,
            )
        session.rollback()
        job = session.get(SearchJob, search_job_id)
        if job is not None:
            job.status = JobStatus.FAILED
            job.error_message = job_error.message
            job.error_detail = job_error.model_dump()
            job.finished_at = datetime.now(timezone.utc)
            _commit_job(session, job)
        telemetry.job_failed(error_code=job_error.code, message=job_error.message)
        raise
    finally:
        session.close()


def _commit_job(session, job: SearchJob) -> None:
    session.add(job)
    session.commit()


TOOL_SPLIT_PATTERN = re.compile(r"(?:/|,|\+|&|\band\b)", re.IGNORECASE)


def _normalize_required_tools(values: Sequence[str] | None) -> list[str]:
    """Normalize automation tool names for consistent analytics."""

    if not values:
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for entry in values:
        if entry is None:
            continue
        chunks = TOOL_SPLIT_PATTERN.split(entry)
        if not chunks:
            chunks = [entry]
        for chunk in chunks:
            tool = chunk.strip().lower()
            if not tool or tool in seen:
                continue
            seen.add(tool)
            normalized.append(tool)
    return normalized


def _persisted_report_from(report: PostReport, job_id: int | None) -> PersistedPostReport:
    if job_id is None:  # pragma: no cover - defensive
        raise RuntimeError("Search job must be stored before persisting reports")
    return PersistedPostReport(
        search_job_id=job_id,
        submission_id=report.submission_id,
        subreddit=report.subreddit,
        title=report.title,
        url=report.url,
        permalink=report.permalink,
        created=report.created,
        created_utc=report.created_utc,
        score=report.score,
        num_comments=report.num_comments,
        automation_complexity=report.automation_insight.automation_complexity,
        required_tools=_normalize_required_tools(
            report.automation_insight.required_tools
        ),
        insight_text=report.automation_insight.deep_analysis,
    )


def _flush_batch(
    session,
    job: SearchJob,
    batch: List[PersistedPostReport],
    *,
    chunk_size: int = 10,
    force: bool = False,
) -> None:
    if not batch:
        return
    if force or len(batch) >= chunk_size:
        session.add_all(batch)
        session.add(job)
        session.commit()
        batch.clear()


__all__ = ["run"]
