"""Web dashboard for viewing Reddit automation reports."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Sequence

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import (
    Body,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jose import JWTError, jwt
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from redis.exceptions import RedisError
from rq import Queue
from sqlalchemy import func
from sqlmodel import SQLModel, Session, delete, select
from starlette.middleware.sessions import SessionMiddleware

from src.api.schemas import (
    AppConfigResponse,
    PersistedPostReportSchema,
    SearchJobListResponse,
    SearchJobResponse,
    SearchJobStats,
    SearchRequest,
    SessionResponse,
    SessionUsage,
    SessionUser,
)
from src.db.migrations import run_migrations
from src.db.models import (
    AuthProvider,
    JobStatus,
    PersistedPostReport,
    SearchJob,
    SubscriptionPlan,
    User,
)
from src.db.repositories import user_repo
from src.db.session import engine, get_session
from src.env import REQUIRED_SECRETS, _require_env, ensure_required_secrets
from src.infra.cleanup import purge_expired_jobs_by_plan
from src.infra.observability import log_structured, set_queue_depth
from src.infra.redis import get_redis_client
from src.infra.search_cache import (
    fetch_cached_job_response,
    remember_search_job_response,
)
from src.infra.search_jobs import (
    count_active_jobs,
    count_jobs_created_since,
    create_search_job,
)
from src.jobs import search_runner
from src.jobs.job_errors import JobError
from src import config as app_config
from src.services.job_stats import build_search_job_stats
from src.services.plans import archival_plans, get_plan_policy, plan_retention_days

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
JOB_CLEANUP_INTERVAL_SECONDS = int(os.getenv("JOB_CLEANUP_INTERVAL_SECONDS", "3600"))
ENABLE_JOB_CLEANUP = os.getenv("ENABLE_JOB_CLEANUP", "true").lower() != "false"

AUTH_COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "reddash_auth")
AUTH_COOKIE_DOMAIN = os.getenv("AUTH_COOKIE_DOMAIN")
AUTH_COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "false").lower() == "true"
AUTH_TOKEN_TTL_SECONDS = int(os.getenv("AUTH_TOKEN_TTL_SECONDS", "86400"))
AUTH_ALGORITHM = "HS256"
PASSWORD_HASH_NAME = os.getenv("PASSWORD_HASH_NAME", "sha256")
PASSWORD_ITERATIONS = int(os.getenv("PASSWORD_ITERATIONS", "130000"))
PASSWORD_SALT_BYTES = int(os.getenv("PASSWORD_SALT_BYTES", "16"))

API_REQUIRED_SECRETS = (
    *REQUIRED_SECRETS,
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "AUTH_SECRET_KEY",
)
ensure_required_secrets(API_REQUIRED_SECRETS)

GOOGLE_CLIENT_ID = _require_env("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = _require_env("GOOGLE_CLIENT_SECRET")
AUTH_SECRET_KEY = _require_env("AUTH_SECRET_KEY")

logger = logging.getLogger(__name__)

app = FastAPI(title="Reddit Automation Report Dashboard")
app.add_middleware(
    SessionMiddleware,
    secret_key=AUTH_SECRET_KEY,
    session_cookie="reddash_state",
    max_age=AUTH_TOKEN_TTL_SECONDS,
    same_site="lax",
    https_only=AUTH_COOKIE_SECURE,
)

oauth = OAuth()
oauth.register(
    "google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

redis_client = get_redis_client()
queue = Queue("reddit-searches", connection=redis_client)


@app.on_event("startup")
def _init_db() -> None:
    """Ensure SQLModel tables exist before serving traffic."""

    run_migrations(engine)


async def _cleanup_loop() -> None:
    while True:
        try:
            purge_expired_jobs_by_plan(
                plan_retention_days(), archive_only_plans=archival_plans()
            )
        except Exception:  # pragma: no cover - defensive logging
            logger.exception("job_cleanup_failed")
        await asyncio.sleep(max(JOB_CLEANUP_INTERVAL_SECONDS, 60))


@app.on_event("startup")
async def _schedule_cleanup() -> None:
    """Launch recurring cleanup of expired jobs per subscription plan."""

    if not ENABLE_JOB_CLEANUP or JOB_CLEANUP_INTERVAL_SECONDS <= 0:
        return
    asyncio.create_task(_cleanup_loop())


def _error_detail(code: str, message: str, *, field: str | None = None) -> Dict[str, str]:
    payload = {"code": code, "message": message}
    if field:
        payload["field"] = field
    return payload


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _generate_password_salt() -> str:
    return secrets.token_bytes(PASSWORD_SALT_BYTES).hex()


def _hash_password(password: str, *, salt_hex: str) -> str:
    return user_repo.derive_password_hash(
        password,
        salt_hex=salt_hex,
        hash_name=PASSWORD_HASH_NAME,
        iterations=PASSWORD_ITERATIONS,
    )


class EmailAuthRequest(BaseModel):
    email: str
    password: str


class EmailSignupRequest(EmailAuthRequest):
    display_name: str | None = None


def _email_auth_request(
    payload: EmailAuthRequest | None = Body(None),
    form_email: str | None = Form(None),
    form_password: str | None = Form(None),
) -> EmailAuthRequest:
    """Accept either JSON or form-encoded login submissions."""

    if payload is not None:
        return payload

    if form_email is not None or form_password is not None:
        return EmailAuthRequest(email=form_email or "", password=form_password or "")

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=_error_detail(
            "invalid_login_payload",
            "Email and password are required to sign in.",
        ),
    )


def enforce_rate_limit(request: Request) -> None:
    """Naive per-IP rate limiting backed by Redis."""

    identifier = request.client.host if request.client else "anonymous"
    window = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
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


def _create_session_token(user: User) -> str:
    """Issue a short-lived JWT session token for the given user."""

    if user.id is None:
        raise RuntimeError("User must be persisted before creating a session token")

    provider = user.auth_provider
    if isinstance(provider, str):
        try:
            provider = AuthProvider(provider)
        except ValueError:
            # Persist whatever value we have to avoid blocking login flows.
            pass

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=AUTH_TOKEN_TTL_SECONDS)
    payload = {
        "sub": str(user.id),
        "provider": provider.value if isinstance(provider, AuthProvider) else str(provider),
        "email": user.email,
        "name": user.display_name,
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(payload, AUTH_SECRET_KEY, algorithm=AUTH_ALGORITHM)


def _set_auth_cookie(response: Response, token: str) -> None:
    """Attach the session JWT to the response as a cookie."""

    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        max_age=AUTH_TOKEN_TTL_SECONDS,
        httponly=True,
        secure=AUTH_COOKIE_SECURE,
        samesite="lax",
        domain=AUTH_COOKIE_DOMAIN,
        path="/",
    )


def _clear_auth_cookie(response: Response) -> None:
    """Remove the session JWT from the browser."""

    response.delete_cookie(
        AUTH_COOKIE_NAME,
        domain=AUTH_COOKIE_DOMAIN,
        path="/",
    )


def _unauthorized_error(detail: str = "Authentication required.") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_error_detail("unauthorized", detail),
    )


def _resolve_user_from_cookie(request: Request) -> User | None:
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token:
        return None

    try:
        payload = jwt.decode(token, AUTH_SECRET_KEY, algorithms=[AUTH_ALGORITHM])
        user_id = int(payload.get("sub"))
    except (JWTError, TypeError, ValueError):
        return None

    with get_session() as session:
        user = user_repo.get_user_by_id(session, user_id)
        if user is None or not user.is_active:
            return None
        session.refresh(user)
        request.state.current_user = user
        return user


def require_authenticated_user(request: Request) -> User:
    """Ensure a valid session token is present and return the user row."""

    user = _resolve_user_from_cookie(request)
    if user is None:
        raise _unauthorized_error()
    return user


def _session_user_payload(user: User) -> SessionUser:
    if user.id is None:
        raise RuntimeError("User must be persisted before building a session payload")

    return SessionUser(
        id=int(user.id),
        display_name=user.display_name,
        email=user.email,
        avatar_url=user.avatar_url,
        subscription_plan=user.subscription_plan,
    )


def _session_usage(user: User) -> SessionUsage:
    policy = get_plan_policy(user.subscription_plan)
    window_start = datetime.now(timezone.utc) - timedelta(days=1)
    jobs_today = count_jobs_created_since(user_id=int(user.id), since_utc=window_start)
    active_jobs = count_active_jobs(user_id=int(user.id))
    remaining = None
    if policy.daily_job_limit is not None:
        remaining = max(policy.daily_job_limit - jobs_today, 0)

    return SessionUsage(
        jobs_today=jobs_today,
        daily_limit=policy.daily_job_limit,
        jobs_remaining=remaining,
        active_jobs=active_jobs,
        concurrent_limit=policy.concurrent_job_limit,
    )


def _session_response(request: Request, user: User | None) -> SessionResponse:
    login_url = str(request.url_for("auth_login_google"))
    if user is None:
        return SessionResponse(authenticated=False, login_url=login_url)

    return SessionResponse(
        authenticated=True,
        login_url=login_url,
        user=_session_user_payload(user),
        usage=_session_usage(user),
    )


def _validate_password_strength(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_error_detail(
                "weak_password",
                "Password must be at least 8 characters long.",
                field="password",
            ),
        )


@app.get("/auth/login/google")
async def auth_login_google(request: Request) -> Response:
    """Initiate the Google OAuth login redirect."""

    redirect_uri = str(request.url_for("auth_google_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.post("/auth/signup", response_model=SessionResponse)
def auth_signup(
    payload: EmailSignupRequest,
    request: Request,
    response: Response,
    _: None = Depends(enforce_rate_limit),
) -> SessionResponse:
    """Create a local email/password account and issue a session cookie."""

    email = _normalize_email(payload.email)
    if not email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_error_detail("invalid_email", "Email is required.", field="email"),
        )
    _validate_password_strength(payload.password)

    salt_hex = _generate_password_salt()
    password_hash = _hash_password(payload.password, salt_hex=salt_hex)
    display_name = payload.display_name or email

    with get_session() as session:
        try:
            user = user_repo.create_local_user(
                session,
                email=email,
                password_hash=password_hash,
                password_salt=salt_hex,
                display_name=display_name,
                subscription_plan=SubscriptionPlan.FREE,
            )
            session.commit()
            session.refresh(user)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=_error_detail(
                    "email_taken",
                    "An account with that email already exists.",
                    field="email",
                ),
            ) from exc
        except SQLAlchemyError as exc:  # pragma: no cover - depends on DB availability
            session.rollback()
            logger.exception("signup_db_error")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=_error_detail(
                    "signup_failed",
                    "Unable to create your account right now. Please try again later.",
                ),
            ) from exc

    jwt_token = _create_session_token(user)
    _set_auth_cookie(response, jwt_token)
    return _session_response(request, user)


@app.post("/auth/login", response_model=SessionResponse)
def auth_login(
    request: Request,
    response: Response,
    payload: EmailAuthRequest = Depends(_email_auth_request),
    _: None = Depends(enforce_rate_limit),
) -> SessionResponse:
    """Authenticate with email/password credentials."""

    email = _normalize_email(payload.email)
    if not email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_error_detail("invalid_email", "Email is required.", field="email"),
        )

    with get_session() as session:
        try:
            user = user_repo.verify_user_credentials(
                session,
                email=email,
                password=payload.password,
                hash_name=PASSWORD_HASH_NAME,
                iterations=PASSWORD_ITERATIONS,
            )
            if user is None:
                raise _unauthorized_error("Invalid email or password.")
            session.commit()
            session.refresh(user)
        except SQLAlchemyError as exc:  # pragma: no cover - depends on DB availability
            session.rollback()
            logger.exception("login_db_error")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=_error_detail(
                    "login_failed",
                    "Unable to sign you in right now. Please try again later.",
                ),
            ) from exc

    jwt_token = _create_session_token(user)
    _set_auth_cookie(response, jwt_token)
    return _session_response(request, user)


@app.get("/auth/callback/google", name="auth_google_callback")
async def auth_google_callback(request: Request) -> Response:
    """Handle Google OAuth callback, issue a JWT, and redirect home."""

    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as exc:  # pragma: no cover - network dependency
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_error_detail("oauth_error", f"Google login failed: {exc.error}"),
        ) from exc

    userinfo = token.get("userinfo") if isinstance(token, dict) else None
    if not userinfo:
        try:
            userinfo = await oauth.google.parse_id_token(request, token)
        except Exception as exc:  # pragma: no cover - network dependency
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=_error_detail("oauth_error", f"Unable to parse Google profile: {exc}"),
            ) from exc

    provider_account_id = (userinfo or {}).get("sub")
    if not provider_account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_error_detail("oauth_error", "Google response missing subject claim."),
        )

    email = userinfo.get("email") if userinfo else None
    display_name = userinfo.get("name") if userinfo else None
    avatar_url = userinfo.get("picture") if userinfo else None

    with get_session() as session:
        user = user_repo.upsert_user_from_identity(
            session,
            provider=AuthProvider.GOOGLE,
            provider_account_id=str(provider_account_id),
            email=email,
            display_name=display_name,
            avatar_url=avatar_url,
        )
        session.add(user)
        session.commit()
        session.refresh(user)

    jwt_token = _create_session_token(user)
    response = RedirectResponse(url="/")
    _set_auth_cookie(response, jwt_token)
    return response


@app.get("/auth/logout")
def auth_logout(request: Request) -> Response:
    """Clear session cookies and return to the dashboard shell."""

    response = RedirectResponse(url="/")
    _clear_auth_cookie(response)
    try:
        request.session.clear()
    except Exception:  # pragma: no cover - session backend specific
        pass
    return response


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
            detail=_error_detail("unauthorized", "User session is not initialized."),
        )

    if policy.daily_job_limit is not None and policy.daily_job_limit > 0:
        window_start = datetime.now(timezone.utc) - timedelta(days=1)
        recent_jobs = count_jobs_created_since(
            user_id=user.id, since_utc=window_start
        )
        if recent_jobs >= policy.daily_job_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=_error_detail(
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
                detail=_error_detail(
                    "job_quota_exceeded",
                    "Too many searches are currently running for your plan. "
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


@app.get("/api/session", response_model=SessionResponse)
def read_session(request: Request) -> SessionResponse:
    """Return authentication state and per-user quota usage."""

    user = _resolve_user_from_cookie(request)
    return _session_response(request, user)


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
    current_user: User = Depends(require_authenticated_user),
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
        user_id=current_user.id,
    )
    if cached_job is not None:
        return cached_job

    _enforce_user_job_quotas(current_user)

    job = create_search_job(
        user_id=int(current_user.id),
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
            user_id=current_user.id,
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
def read_search(
    job_id: int, current_user: User = Depends(require_authenticated_user)
) -> SearchJobResponse:
    with get_session() as session:
        job = _get_job_or_404(session, job_id, user_id=current_user.id)
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
    current_user: User = Depends(require_authenticated_user),
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


@app.get("/metrics")
def metrics() -> Response:
    """Expose Prometheus metrics for scraping."""

    payload = generate_latest()
    return Response(payload, media_type=CONTENT_TYPE_LATEST)


@app.delete("/api/searches/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_search(
    job_id: int, current_user: User = Depends(require_authenticated_user)
) -> None:
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


def _page_response(request: Request, template_name: str) -> HTMLResponse:
    """Render a Jinja template with navigation context."""

    return TEMPLATES.TemplateResponse(
        template_name,
        {"request": request, "current_path": request.url.path},
    )


@app.get("/", response_class=HTMLResponse)
def render_landing(request: Request) -> HTMLResponse:
    """Landing page that introduces the multipage dashboard."""

    return _page_response(request, "landing.html")


@app.get("/auth", response_class=HTMLResponse)
def render_auth(request: Request) -> HTMLResponse:
    """Login and signup experience for RedDash."""

    return _page_response(request, "auth.html")


@app.get("/plans", response_class=HTMLResponse)
def render_plan_selection(request: Request) -> HTMLResponse:
    """Let visitors choose a subscription plan before running searches."""

    return _page_response(request, "plan_selection.html")


@app.get("/dashboard", response_class=HTMLResponse)
def render_dashboard(request: Request) -> HTMLResponse:
    """Primary dashboard surface for launching and reviewing jobs."""

    return _page_response(request, "dashboard.html")


@app.get("/preferences", response_class=HTMLResponse)
def render_preferences(request: Request) -> HTMLResponse:
    """User preferences page for notifications and defaults."""

    return _page_response(request, "preferences.html")


__all__ = ["app", "get_reports", "PostReport", "SearchRequest"]
