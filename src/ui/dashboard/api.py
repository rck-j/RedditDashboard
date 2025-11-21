"""API router for JSON endpoints used by the dashboard."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Sequence

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response, status
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, delete, select

from src.api.schemas import (
    AppConfigResponse,
    PersistedPostReportSchema,
    SearchJobListResponse,
    SearchJobResponse,
    SearchJobStats,
    SearchRequest,
    SessionResponse,
)
from src.db.models import JobStatus, PersistedPostReport, SearchJob, User
from src.db.session import get_session
from src import config as app_config
from src.infra.observability import log_structured, set_queue_depth
from src.infra.search_cache import fetch_cached_job_response, remember_search_job_response
from src.infra.search_jobs import count_active_jobs, count_jobs_created_since, create_search_job
from src.jobs import search_runner
from src.jobs.job_errors import JobError
from src.services.job_stats import build_search_job_stats
from src.services.plans import get_plan_policy
from src.ui.dashboard import settings
from src.ui.dashboard.auth import require_authenticated_user, session_response
from src.ui.dashboard.common import enforce_rate_limit, error_detail
from src.ui.dashboard.dependencies import queue, redis_client
from src.ui.dashboard.reports import PostReport, get_reports

logger = logging.getLogger(__name__)

api_router = APIRouter(prefix="/api")


def _cache_job_response(job: SearchJob, response: SearchJobResponse) -> None:
    """Persist a sanitized snapshot of the job in the Redis cache."""

    from redis.exceptions import RedisError

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
            user_id=job.user_id,
        )
    except RedisError as exc:  # pragma: no cover - depends on Redis availability
        logger.warning("Unable to cache job %s: %s", job.id, exc)


def _enforce_user_job_quotas(user: User) -> None:
    """Apply per-plan limits before enqueueing work for a user."""

    policy = get_plan_policy(user.subscription_plan)
    if user.id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_detail("unauthorized", "User session is not initialized."),
        )

    if policy.daily_job_limit is not None and policy.daily_job_limit > 0:
        window_start = datetime.now(timezone.utc) - timedelta(days=1)
        recent_jobs = count_jobs_created_since(
            user_id=user.id, since_utc=window_start
        )
        if recent_jobs >= policy.daily_job_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=error_detail(
                    "daily_quota_exceeded",
                    "Daily search quota exceeded for your plan. "
                    f"Limit: {policy.daily_job_limit} per 24 hours.",
                ),
            )

    if policy.concurrent_job_limit is not None and policy.concurrent_job_limit > 0:
        active_jobs = count_active_jobs(user_id=user.id)
        if active_jobs >= policy.concurrent_job_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=error_detail(
                    "job_quota_exceeded",
                    "Too many searches are currently running for your plan. "
                    "Please wait for existing jobs to finish before starting another run.",
                ),
            )


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


def _get_job_or_404(session, job_id: int, *, user_id: int | None = None) -> SearchJob:
    job = session.get(SearchJob, job_id)
    if job is None or job.is_deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search job not found.")
    if user_id is not None and job.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search job not found.")
    return job


def _validate_pagination(page: int, page_size: int) -> None:
    if page <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_detail("invalid_page", "Page must be positive.", field="page"),
        )
    if page_size <= 0 or page_size > 100:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_detail(
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


def _job_response(
    job: SearchJob,
    *,
    session: Session | None = None,
    cache: bool = False,
    reports: Sequence[PersistedPostReport] | None = None,
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


@api_router.get("/reports", response_model=List[PostReport])
def read_reports(response: Response) -> List[PostReport]:
    """Return the parsed report entries backed by the SQLModel database."""

    job, reports = get_reports(return_job=True)
    if job is None:
        response.headers["X-RedDash-Report-Status"] = "no-completed-job"
        response.headers[
            "X-RedDash-Report-Message"
        ] = "No completed search jobs yet. Launch one via POST /api/searches."
        return []

    stats_payload: Dict[str, str | int | None] = {
        "search_job_id": job.id,
        "report_count": len(reports),
        "generated_at": job.finished_at.isoformat() if job.finished_at else None,
        "query": job.query,
    }
    response.headers["X-RedDash-Report-Status"] = "ok"
    response.headers["X-RedDash-Report-Stats"] = json.dumps(stats_payload)
    return reports


@api_router.get("/config", response_model=AppConfigResponse)
def read_app_config() -> AppConfigResponse:
    """Expose shared search and analyzer metadata for the dashboard UI."""

    return AppConfigResponse.model_validate(app_config.get_app_config())


@api_router.get("/session", response_model=SessionResponse)
def read_session(request: Request) -> SessionResponse:
    """Return authentication state and per-user quota usage."""

    from src.ui.dashboard.auth import _resolve_user_from_cookie

    user = _resolve_user_from_cookie(request)
    return session_response(request, user)


@api_router.post(
    "/searches",
    response_model=SearchJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_search(
    request: Request,
    search_request: SearchRequest = Body(...),
    current_user: User = Depends(require_authenticated_user),
    _: None = Depends(enforce_rate_limit),
) -> SearchJobResponse:
    _enforce_user_job_quotas(current_user)

    cache_hit = fetch_cached_job_response(
        redis_client,
        subreddits=search_request.subreddits,
        query=search_request.query,
        time_filter=search_request.time_filter,
        limit=search_request.limit,
        comments_limit=search_request.comments_limit,
        user_id=current_user.id,
    )
    if cache_hit:
        log_structured("search_cached", user_id=current_user.id, query=search_request.query)
        return cache_hit

    with get_session() as session:
        try:
            job = create_search_job(session, search_request, user_id=current_user.id)
            session.commit()
        except SQLAlchemyError as exc:  # pragma: no cover - depends on DB availability
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=error_detail("job_creation_failed", "Unable to queue your search request."),
            ) from exc

    set_queue_depth(queue)
    queue.enqueue(search_runner.run_search_job, job.id, job.user_id)
    log_structured(
        "search_queued",
        user_id=current_user.id,
        search_job_id=job.id,
        query=search_request.query,
    )
    return _job_response(job, cache=True)


@api_router.get("/searches/{job_id}", response_model=SearchJobResponse)
def read_search(job_id: int, current_user: User = Depends(require_authenticated_user)) -> SearchJobResponse:
    with get_session() as session:
        job = _get_job_or_404(session, job_id, user_id=current_user.id)
        reports = _fetch_reports(session, job.id)
        return _job_response(job, session=session, reports=reports)


@api_router.get("/searches", response_model=SearchJobListResponse)
def list_searches(
    current_user: User = Depends(require_authenticated_user),
    status_filter: JobStatus | None = Query(None, description="Filter by search status"),
    search: str | None = Query(None, description="Full text match against query"),
    order: str | None = Query("desc", description="Sort order (asc|desc) for created_at"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> SearchJobListResponse:
    normalized_order = (order or "desc").lower()
    _validate_pagination(page, page_size)
    if normalized_order not in {"asc", "desc"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_detail(
                "invalid_order",
                "Order must be 'asc' or 'desc'.",
                field="order",
            ),
        )

    with get_session() as session:
        base_stmt = select(SearchJob).where(
            SearchJob.is_deleted.is_(False),
            SearchJob.user_id == current_user.id,
        )
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


@api_router.delete("/searches/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_search(job_id: int, current_user: User = Depends(require_authenticated_user)) -> None:
    with get_session() as session:
        job = _get_job_or_404(session, job_id, user_id=current_user.id)
        session.exec(
            delete(PersistedPostReport).where(
                PersistedPostReport.search_job_id == job.id
            )
        )
        job.is_deleted = True
        session.add(job)
        session.commit()


__all__ = ["api_router", "get_reports", "PostReport", "SearchRequest"]
