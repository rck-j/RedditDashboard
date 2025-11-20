"""Web dashboard for viewing Reddit automation reports."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Sequence

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field, ValidationError
from redis.exceptions import RedisError
from rq import Queue
from sqlalchemy import func
from sqlmodel import SQLModel, Session, delete, select

from src.api.schemas import (
    AppConfigResponse,
    PersistedPostReportSchema,
    SearchJobListResponse,
    SearchJobResponse,
    SearchJobStats,
    SearchRequest,
)
from src.db.migrations import run_migrations
from src.db.models import (
    AuthProvider,
    JobStatus,
    PersistedPostReport,
    SearchJob,
    SubscriptionPlan,
)
from src.db.repositories import user_repo
from src.db.session import engine, get_session
from src.env import REQUIRED_SECRETS, ensure_required_secrets
from src.infra.observability import log_structured, set_queue_depth
from src.infra.redis import get_redis_client
from src.infra.search_cache import (
    fetch_cached_job_response,
    remember_search_job_response,
)
from src.infra.search_jobs import count_active_jobs, create_search_job
from src.jobs import search_runner
from src.jobs.job_errors import JobError
from src import config as app_config
from src.services.job_stats import build_search_job_stats

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


ROOT_DIR = Path(__file__).resolve().parents[2]
TEMPLATES = Jinja2Templates(directory=str(ROOT_DIR / "templates"))

RATE_LIMIT_REQUESTS_PER_MINUTE = 30
DEFAULT_MAX_CONCURRENT_JOBS = 3

API_REQUIRED_SECRETS = tuple(REQUIRED_SECRETS)
ensure_required_secrets(API_REQUIRED_SECRETS)

logger = logging.getLogger(__name__)

app = FastAPI(title="Reddit Automation Report Dashboard")
redis_client = get_redis_client()
queue = Queue("reddit-searches", connection=redis_client)


@app.on_event("startup")
def _init_db() -> None:
    """Ensure SQLModel tables exist before serving traffic."""

    run_migrations(engine)


def _error_detail(code: str, message: str, *, field: str | None = None) -> Dict[str, str]:
    payload = {"code": code, "message": message}
    if field:
        payload["field"] = field
    return payload


def enforce_rate_limit(request: Request) -> None:
    """Naive per-IP rate limiting backed by Redis."""

    identifier = request.client.host if request.client else "anonymous"
    window = datetime.utcnow().strftime("%Y%m%d%H%M")
    key = f"rate-limit:{identifier}:{window}"
    try:
        hits = redis_client.incr(key)
        if hits == 1:
            redis_client.expire(key, 60)
        if hits > RATE_LIMIT_REQUESTS_PER_MINUTE:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=_error_detail(
                    "rate_limit_exceeded",
                    "Too many requests; please wait a moment before retrying.",
                ),
            )
    except RedisError:  # pragma: no cover - network-dependent
        # Fall back to allowing the request when Redis is unavailable.
        return


def _resolve_request_user_id(request: Request | None) -> int:
    """Resolve the authenticated user from headers, defaulting to the system user."""

    provider_name = None
    account_id = None
    display_name = None
    email = None
    avatar_url = None
    plan_name = None
    if request is not None:
        headers = request.headers
        provider_name = headers.get("X-RedDash-Auth-Provider")
        account_id = (
            headers.get("X-RedDash-Auth-Subject")
            or headers.get("X-RedDash-Auth-Id")
        )
        display_name = headers.get("X-RedDash-Auth-Name")
        email = headers.get("X-RedDash-Auth-Email")
        avatar_url = headers.get("X-RedDash-Auth-Avatar")
        plan_name = headers.get("X-RedDash-Subscription-Plan")

    normalized_provider = (provider_name or "").strip().lower().replace("-", "_")
    provider = AuthProvider.SYSTEM
    if normalized_provider:
        try:
            provider = AuthProvider(normalized_provider)
        except ValueError:
            provider = AuthProvider.CUSTOM

    normalized_plan = (plan_name or "").strip().lower()
    subscription_plan: SubscriptionPlan | None = None
    if normalized_plan:
        try:
            subscription_plan = SubscriptionPlan(normalized_plan)
        except ValueError:
            subscription_plan = None

    resolved_account_id = (account_id or "system").strip() or "system"
    with get_session() as session:
        user = user_repo.upsert_user_from_identity(
            session,
            provider=provider,
            provider_account_id=resolved_account_id,
            email=email,
            display_name=display_name,
            avatar_url=avatar_url,
            subscription_plan=subscription_plan,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        if user.id is None:  # pragma: no cover - defensive
            raise RuntimeError("User persistence failed")
        return user.id


def _cache_job_response(job: SearchJob, response: SearchJobResponse) -> None:
    """Persist a sanitized snapshot of the job in the Redis cache."""

    try:
        payload = response
        if response.reports:
            payload = response.model_copy(update={"reports": None})
        remember_search_job_response(
            redis_client,
            job_response=payload,
            subreddits=job.subreddits,
            query=job.query,
            time_filter=job.time_filter,
            limit=job.limit,
            comments_limit=job.comments_limit,
        )
    except RedisError as exc:  # pragma: no cover - depends on Redis availability
        logger.warning("Unable to cache job %s: %s", job.id, exc)


def _max_concurrent_jobs() -> int:
    raw_value = os.getenv("MAX_CONCURRENT_JOBS")
    try:
        value = int(raw_value) if raw_value is not None else DEFAULT_MAX_CONCURRENT_JOBS
    except ValueError:
        value = DEFAULT_MAX_CONCURRENT_JOBS
    return max(value, 0)


def _enforce_concurrent_job_quota() -> None:
    limit = _max_concurrent_jobs()
    if limit <= 0:
        return
    active_jobs = count_active_jobs()
    if active_jobs >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_error_detail(
                "job_quota_exceeded",
                "Too many searches are currently running. "
                "Please wait for existing jobs to finish before starting another run.",
            ),
        )


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


def get_reports() -> List[PostReport]:
    """Return the latest completed search as legacy PostReports."""

    _, reports = _latest_reports()
    return reports


@app.get("/api/reports", response_model=List[PostReport])
def read_reports(response: Response) -> List[PostReport]:
    """Return the parsed report entries backed by the SQLModel database."""

    job, reports = _latest_reports()
    if job is None:
        response.headers["X-RedDash-Report-Status"] = "no-completed-job"
        response.headers[
            "X-RedDash-Report-Message"
        ] = "No completed search jobs yet. Launch one via POST /api/searches."
        return []

    stats_payload = {
        "search_job_id": job.id,
        "report_count": len(reports),
        "generated_at": job.finished_at.isoformat() if job.finished_at else None,
        "query": job.query,
    }
    response.headers["X-RedDash-Report-Status"] = "ok"
    response.headers["X-RedDash-Report-Stats"] = json.dumps(stats_payload)
    return reports


@app.get("/api/config", response_model=AppConfigResponse)
def read_app_config() -> AppConfigResponse:
    """Expose shared search and analyzer metadata for the dashboard UI."""

    return AppConfigResponse.model_validate(app_config.get_app_config())


def _job_response(
    job: SearchJob,
    *,
    session: Session | None = None,
    reports: Sequence[PersistedPostReport] | None = None,
    cache: bool = False,
) -> SearchJobResponse:
    stats_payload = _resolve_stats(job, session=session, reports=reports)
    stats = SearchJobStats.model_validate(stats_payload)
    error_payload = _deserialize_job_error(job)
    response = SearchJobResponse(
        id=job.id,
        subreddits=job.subreddits,
        query=job.query,
        time_filter=job.time_filter,
        limit=job.limit,
        comments_limit=job.comments_limit,
        status=job.status,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error_message=job.error_message,
        error=error_payload,
        has_partial_results=_has_partial_results(job),
        stats=stats,
        reports=
        [
            PersistedPostReportSchema.model_validate(report)
            for report in reports
        ]
        if reports is not None
        else None,
    )
    if cache:
        _cache_job_response(job, response)
    return response


def _deserialize_job_error(job: SearchJob) -> JobError | None:
    if not job.error_detail:
        return None
    try:
        return JobError.model_validate(job.error_detail)
    except ValidationError:
        fallback_message = job.error_message or "Unknown failure"
        return JobError(code="unknown", message=fallback_message)


def _has_partial_results(job: SearchJob) -> bool:
    return job.status == JobStatus.FAILED and job.processed_count > 0


def _resolve_stats(
    job: SearchJob,
    *,
    session: Session | None = None,
    reports: Sequence[PersistedPostReport] | None = None,
) -> dict:
    if job.status == JobStatus.SUCCEEDED:
        if job.stats:
            return job.stats
        active_session = session
        owns_session = False
        if active_session is None:
            active_session = get_session()
            owns_session = True
        try:
            stats = build_search_job_stats(active_session, job, reports=reports)
            if session is not None:
                job.stats = stats
                active_session.add(job)
                active_session.commit()
            return stats
        finally:
            if owns_session and active_session is not None:
                active_session.close()

    return {
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


def _get_job_or_404(session, job_id: int) -> SearchJob:
    job = session.get(SearchJob, job_id)
    if job is None or job.is_deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search job not found.")
    return job


def _validate_pagination(page: int, page_size: int) -> None:
    if page <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_error_detail("invalid_page", "Page must be positive.", field="page"),
        )
    if page_size <= 0 or page_size > 100:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_error_detail(
                "invalid_page_size",
                "Page size must be between 1 and 100.",
                field="page_size",
            ),
        )


def _fetch_reports(session, job_id: int) -> List[PersistedPostReport]:
    return list(
        session.exec(
            select(PersistedPostReport)
            .where(PersistedPostReport.search_job_id == job_id)
            .order_by(PersistedPostReport.created_at.desc())
        )
    )


@app.post(
    "/api/searches",
    response_model=SearchJobResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_search(
    request: SearchRequest,
    http_request: Request,
    _: None = Depends(enforce_rate_limit),
) -> SearchJobResponse:
    """Queue a Reddit search via RQ and return the job metadata."""

    payload = request
    cached_job = fetch_cached_job_response(
        redis_client,
        subreddits=payload.subreddits,
        query=payload.query,
        time_filter=payload.time_filter,
        limit=payload.limit,
        comments_limit=payload.comments_limit,
    )
    if cached_job is not None:
        return cached_job

    _enforce_concurrent_job_quota()

    user_id = _resolve_request_user_id(http_request)
    job = create_search_job(
        user_id=user_id,
        query=payload.query,
        subreddits=payload.subreddits,
        time_filter=payload.time_filter,
        limit=payload.limit,
        comments_limit=payload.comments_limit,
    )
    try:
        queue.enqueue(
            search_runner.run,
            job_timeout=900,
            search_job_id=job.id,
            subreddits=payload.subreddits,
            query=payload.query,
            time_filter=payload.time_filter,
            limit=payload.limit,
            comments_limit=payload.comments_limit,
        )
        try:
            current_depth = len(queue)
        except Exception:  # pragma: no cover - depends on Redis availability
            current_depth = None
        else:
            set_queue_depth(current_depth)
        log_structured(
            logger,
            logging.INFO,
            "job_enqueued",
            job_id=job.id,
            user_id=user_id,
            queue_depth=current_depth,
        )
    except Exception as exc:  # pragma: no cover - depends on Redis availability
        log_structured(
            logger,
            logging.ERROR,
            "job_enqueue_failed",
            job_id=job.id,
            message=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Unable to enqueue search job: {exc}",
        ) from exc

    return _job_response(job, cache=True)


@app.get("/api/searches/{job_id}", response_model=SearchJobResponse)
def read_search(job_id: int) -> SearchJobResponse:
    with get_session() as session:
        job = _get_job_or_404(session, job_id)
        reports: List[PersistedPostReport] | None = None
        if job.status == JobStatus.SUCCEEDED:
            reports = _fetch_reports(session, job.id)
        return _job_response(job, session=session, reports=reports, cache=True)


@app.get("/api/searches", response_model=SearchJobListResponse)
def list_searches(
    *,
    status_filter: JobStatus | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 20,
    order: str = "desc",
) -> SearchJobListResponse:
    _validate_pagination(page, page_size)
    normalized_order = order.lower()
    if normalized_order not in {"asc", "desc"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_error_detail(
                "invalid_order",
                "Order must be 'asc' or 'desc'.",
                field="order",
            ),
        )

    with get_session() as session:
        base_stmt = select(SearchJob).where(SearchJob.is_deleted.is_(False))
        if status_filter is not None:
            base_stmt = base_stmt.where(SearchJob.status == status_filter)
        if search:
            base_stmt = base_stmt.where(SearchJob.query.contains(search))

        total = session.exec(
            select(func.count()).select_from(base_stmt.subquery())
        ).one()
        order_clause = (
            SearchJob.created_at.desc()
            if normalized_order == "desc"
            else SearchJob.created_at
        )
        paged_stmt = (
            base_stmt.order_by(order_clause)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        jobs = list(session.exec(paged_stmt))
        return SearchJobListResponse(
            total=int(total or 0),
            page=page,
            page_size=page_size,
            items=[
                _job_response(job, session=session, cache=True)
                for job in jobs
            ],
        )


@app.get("/metrics")
def metrics() -> Response:
    """Expose Prometheus metrics for scraping."""

    payload = generate_latest()
    return Response(payload, media_type=CONTENT_TYPE_LATEST)


@app.delete("/api/searches/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_search(job_id: int) -> None:
    with get_session() as session:
        job = _get_job_or_404(session, job_id)
        session.exec(
            delete(PersistedPostReport).where(
                PersistedPostReport.search_job_id == job.id
            )
        )
        job.is_deleted = True
        session.add(job)
        session.commit()


@app.get("/", response_class=HTMLResponse)
def render_dashboard(request: Request) -> HTMLResponse:
    """Render the dashboard shell; client fetches data via HTMX."""

    return TEMPLATES.TemplateResponse(
        "report_dashboard.html",
        {"request": request},
    )


__all__ = ["app", "get_reports", "PostReport", "SearchRequest"]
