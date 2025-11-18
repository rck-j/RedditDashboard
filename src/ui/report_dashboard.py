"""Web dashboard for viewing Reddit automation reports."""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError
from redis.exceptions import RedisError
from rq import Queue
from sqlalchemy import func
from sqlmodel import SQLModel, delete, select

from src.api.schemas import (
    AppConfigResponse,
    PersistedPostReportSchema,
    SearchJobListResponse,
    SearchJobResponse,
    SearchJobStats,
    SearchRequest,
)
from src.db.models import JobStatus, PersistedPostReport, SearchJob
from src.db.session import engine, get_session
from src.infra.redis import get_redis_client
from src.infra.search_cache import fetch_cached_job, remember_search_job
from src.infra.search_jobs import create_search_job
from src.jobs import search_runner
from src import config as app_config

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
        score: int
        num_comments: int
        initial_assessment: InitialAssessment
        automation_insight: AutomationInsight


class PostReport(BasePostReport):
    """Local alias to ensure consistent validation."""


ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_PATH = ROOT_DIR / "data" / "report.json"
TEMPLATES = Jinja2Templates(directory=str(ROOT_DIR / "templates"))

RATE_LIMIT_REQUESTS_PER_MINUTE = 30

app = FastAPI(title="Reddit Automation Report Dashboard")
redis_client = get_redis_client()
queue = Queue("reddit-searches", connection=redis_client)


@app.on_event("startup")
def _init_db() -> None:
    """Ensure SQLModel tables exist before serving traffic."""

    SQLModel.metadata.create_all(engine)


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:  # pragma: no cover - runtime protection
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except json.JSONDecodeError as exc:  # pragma: no cover - runtime protection
        raise HTTPException(status_code=500, detail="Invalid report JSON") from exc


def _error_detail(code: str, message: str, *, field: str | None = None) -> Dict[str, str]:
    payload = {"code": code, "message": message}
    if field:
        payload["field"] = field
    return payload


def _parse_reports(items: Iterable[Dict[str, Any]]) -> List[PostReport]:
    reports: List[PostReport] = []
    for item in items:
        try:
            reports.append(PostReport.model_validate(item))
        except ValidationError as exc:  # pragma: no cover - runtime protection
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    return reports


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


@lru_cache(maxsize=1)
def get_reports() -> List[PostReport]:
    """Load and parse report entries from disk."""

    payload = _load_json(DATA_PATH)
    posts = payload.get("posts")
    if posts is None:
        if isinstance(payload, list):
            posts = payload
        else:
            raise HTTPException(status_code=500, detail="Report payload missing 'posts'.")
    if not isinstance(posts, list):
        raise HTTPException(status_code=500, detail="Report posts should be a list.")
    return _parse_reports(posts)


@app.get("/api/reports", response_model=List[PostReport])
def read_reports() -> List[PostReport]:
    """Return the parsed report entries as JSON."""

    return get_reports()


@app.get("/api/config", response_model=AppConfigResponse)
def read_app_config() -> AppConfigResponse:
    """Expose shared search and analyzer metadata for the dashboard UI."""

    return AppConfigResponse.model_validate(app_config.get_app_config())


def _job_response(
    job: SearchJob,
    *,
    reports: Sequence[PersistedPostReport] | None = None,
) -> SearchJobResponse:
    report_count = (
        len(reports)
        if reports is not None
        else (job.processed_count if job.status == JobStatus.SUCCEEDED else 0)
    )
    stats = SearchJobStats(
        processed_count=job.processed_count,
        total_count=job.total_count,
        average_score=job.average_score,
        report_count=report_count,
    )
    return SearchJobResponse(
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
        stats=stats,
        reports=
        [
            PersistedPostReportSchema.model_validate(report)
            for report in reports
        ]
        if reports is not None
        else None,
    )


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
    _: None = Depends(enforce_rate_limit),
) -> SearchJobResponse:
    """Queue a Reddit search via RQ and return the job metadata."""

    payload = request
    cached_job = fetch_cached_job(
        redis_client,
        subreddits=payload.subreddits,
        query=payload.query,
        time_filter=payload.time_filter,
        limit=payload.limit,
        comments_limit=payload.comments_limit,
    )
    if cached_job is not None:
        return _job_response(cached_job)

    job = create_search_job(
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
    except Exception as exc:  # pragma: no cover - depends on Redis availability
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Unable to enqueue search job: {exc}",
        ) from exc

    remember_search_job(
        redis_client,
        job_id=job.id,
        subreddits=payload.subreddits,
        query=payload.query,
        time_filter=payload.time_filter,
        limit=payload.limit,
        comments_limit=payload.comments_limit,
    )
    return _job_response(job)


@app.get("/api/searches/{job_id}", response_model=SearchJobResponse)
def read_search(job_id: int) -> SearchJobResponse:
    with get_session() as session:
        job = _get_job_or_404(session, job_id)
        reports: List[PersistedPostReport] | None = None
        if job.status == JobStatus.SUCCEEDED:
            reports = _fetch_reports(session, job.id)
        return _job_response(job, reports=reports)


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
            items=[_job_response(job) for job in jobs],
        )


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
