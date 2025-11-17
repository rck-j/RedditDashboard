"""RQ job for running a Reddit automation search."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Sequence

from dotenv import load_dotenv

from red import PROMPTS, build_openai_client, build_reddit_client
from src.db.models import JobStatus, PersistedPostReport, SearchJob
from src.db.session import get_session
from src.services import AnalyzerDependencies, AutomationAnalyzer, PostReport, search_posts

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
    session.add(job)
    session.commit()

    reddit_client = build_reddit_client()
    openai_client, model_name = build_openai_client()
    deps = AnalyzerDependencies(
        reddit=reddit_client,
        openai=openai_client,
        openai_model=model_name,
        prompts=PROMPTS,
    )
    analyzer = AutomationAnalyzer(deps)
    batch: List[PersistedPostReport] = []
    processed = 0
    try:
        for summary in search_posts(
            reddit_client,
            subreddits=subreddits,
            query=query,
            time_filter=time_filter,
            limit=limit,
        ):
            processed += 1
            job.total_count = processed
            report = analyzer.analyze_post(summary, comment_limit=comments_limit)
            batch.append(_persisted_report_from(report, job.id))
            job.processed_count = processed
            _flush_batch(session, job, batch)

        _flush_batch(session, job, batch, force=True)

        job.status = JobStatus.SUCCEEDED
        job.finished_at = datetime.now(timezone.utc)
        session.add(job)
        session.commit()
        return str(job.id)
    except Exception as exc:  # pragma: no cover - depends on live APIs
        session.rollback()
        job = session.get(SearchJob, search_job_id)
        if job is not None:
            job.status = JobStatus.FAILED
            job.error_message = str(exc)
            job.finished_at = datetime.now(timezone.utc)
            session.add(job)
            session.commit()
        raise
    finally:
        session.close()


def _persisted_report_from(report: PostReport, job_id: int | None) -> PersistedPostReport:
    if job_id is None:  # pragma: no cover - defensive
        raise RuntimeError("Search job must be stored before persisting reports")
    return PersistedPostReport(
        search_job_id=job_id,
        subreddit=report.subreddit,
        title=report.title,
        url=report.url,
        created=report.created,
        score=report.score,
        num_comments=report.num_comments,
        initial_assessment=report.initial_assessment.model_dump(),
        automation_insight=report.automation_insight.model_dump(),
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
