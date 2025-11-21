"""Authentication router and helpers for the dashboard."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import List

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from src.api.schemas import SessionResponse, SessionUsage, SessionUser
from src.db.models import AuthProvider, SubscriptionPlan, User
from src.db.repositories import user_repo
from src.db.session import get_session
from src.services.plans import get_plan_policy
from src.ui.dashboard import settings
from src.ui.dashboard.common import enforce_rate_limit, error_detail, wants_html_response

auth_router = APIRouter(prefix="/auth")
oauth = OAuth()
oauth.register(
    "google",
    client_id=settings.GOOGLE_CLIENT_ID,
    client_secret=settings.GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


class EmailAuthRequest(BaseModel):
    email: str
    password: str


class EmailSignupRequest(EmailAuthRequest):
    display_name: str | None = None


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _generate_password_salt() -> str:
    return secrets.token_bytes(settings.PASSWORD_SALT_BYTES).hex()


def _hash_password(password: str, *, salt_hex: str) -> str:
    return user_repo.derive_password_hash(
        password,
        salt_hex=salt_hex,
        hash_name=settings.PASSWORD_HASH_NAME,
        iterations=settings.PASSWORD_ITERATIONS,
    )


def _email_auth_request(
    payload: EmailAuthRequest | None = Body(None),
    email: str | None = Form(None),
    password: str | None = Form(None),
) -> EmailAuthRequest:
    """Accept either JSON or form-encoded login submissions."""

    if payload is not None:
        return payload

    if email is not None or password is not None:
        return EmailAuthRequest(email=email or "", password=password or "")

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=error_detail(
            "invalid_login_payload",
            "Email and password are required to sign in.",
        ),
    )


def _create_session_token(user: User) -> str:
    """Issue a short-lived JWT session token for the given user."""

    if user.id is None:
        raise RuntimeError("User must be persisted before creating a session token")

    provider = user.auth_provider
    if isinstance(provider, str):
        try:
            provider = AuthProvider(provider)
        except ValueError:
            pass

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.AUTH_TOKEN_TTL_SECONDS)
    payload = {
        "sub": str(user.id),
        "provider": provider.value if isinstance(provider, AuthProvider) else str(provider),
        "email": user.email,
        "name": user.display_name,
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(payload, settings.AUTH_SECRET_KEY, algorithm=settings.AUTH_ALGORITHM)


def _set_auth_cookie(response: Response, token: str) -> None:
    """Attach the session JWT to the response as a cookie."""

    response.set_cookie(
        settings.AUTH_COOKIE_NAME,
        token,
        max_age=settings.AUTH_TOKEN_TTL_SECONDS,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite="lax",
        domain=settings.AUTH_COOKIE_DOMAIN,
        path="/",
    )


def _clear_auth_cookie(response: Response) -> None:
    """Remove the session JWT from the browser."""

    response.delete_cookie(
        settings.AUTH_COOKIE_NAME,
        domain=settings.AUTH_COOKIE_DOMAIN,
        path="/",
    )


def _unauthorized_error(detail: str = "Authentication required.") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=error_detail("unauthorized", detail),
    )


def _resolve_user_from_cookie(request: Request) -> User | None:
    token = request.cookies.get(settings.AUTH_COOKIE_NAME)
    if not token:
        return None

    try:
        payload = jwt.decode(token, settings.AUTH_SECRET_KEY, algorithms=[settings.AUTH_ALGORITHM])
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
    from src.infra.search_jobs import count_active_jobs, count_jobs_created_since

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


def session_response(request: Request, user: User | None) -> SessionResponse:
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
            detail=error_detail(
                "weak_password",
                "Password must be at least 8 characters long.",
                field="password",
            ),
        )


@auth_router.get("/login/google")
async def auth_login_google(request: Request) -> Response:
    """Initiate the Google OAuth login redirect."""

    redirect_uri = str(request.url_for("auth_google_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@auth_router.post("/signup", response_model=SessionResponse)
def auth_signup(
    request: Request,
    response: Response,
    payload: EmailSignupRequest | None = Body(None),
    display_name: str | None = Form(None),
    email: str | None = Form(None),
    password: str | None = Form(None),
    _: None = Depends(enforce_rate_limit),
):
    """Create a local email/password account and issue a session cookie."""

    if payload is None:
        if email is None or password is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=error_detail(
                    "invalid_signup_payload",
                    "Email and password are required to create an account.",
                ),
            )
        payload = EmailSignupRequest(
            email=email,
            password=password,
            display_name=display_name,
        )

    normalized_email = _normalize_email(payload.email)
    if not normalized_email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_detail("invalid_email", "Email is required.", field="email"),
        )
    _validate_password_strength(payload.password)

    salt_hex = _generate_password_salt()
    password_hash = _hash_password(payload.password, salt_hex=salt_hex)
    display_name = payload.display_name or normalized_email

    with get_session() as session:
        try:
            user = user_repo.create_local_user(
                session,
                email=normalized_email,
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
                detail=error_detail(
                    "email_taken",
                    "An account with that email already exists.",
                    field="email",
                ),
            ) from exc
        except SQLAlchemyError as exc:  # pragma: no cover - depends on DB availability
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=error_detail(
                    "signup_failed",
                    "Unable to create your account right now. Please try again later.",
                ),
            ) from exc

    jwt_token = _create_session_token(user)
    if wants_html_response(request):
        redirect = RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
        _set_auth_cookie(redirect, jwt_token)
        return redirect

    _set_auth_cookie(response, jwt_token)
    return session_response(request, user)


@auth_router.post("/login", response_model=SessionResponse)
def auth_login(
    request: Request,
    response: Response,
    payload: EmailAuthRequest = Depends(_email_auth_request),
    _: None = Depends(enforce_rate_limit),
):
    """Authenticate with email/password credentials."""

    email = _normalize_email(payload.email)
    if not email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_detail("invalid_email", "Email is required.", field="email"),
        )

    with get_session() as session:
        try:
            user = user_repo.verify_user_credentials(
                session,
                email=email,
                password=payload.password,
                hash_name=settings.PASSWORD_HASH_NAME,
                iterations=settings.PASSWORD_ITERATIONS,
            )
            if user is None:
                raise _unauthorized_error("Invalid email or password.")
            session.commit()
            session.refresh(user)
        except SQLAlchemyError as exc:  # pragma: no cover - depends on DB availability
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=error_detail(
                    "login_failed",
                    "Unable to sign you in right now. Please try again later.",
                ),
            ) from exc

    jwt_token = _create_session_token(user)
    if wants_html_response(request):
        redirect = RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
        _set_auth_cookie(redirect, jwt_token)
        return redirect

    _set_auth_cookie(response, jwt_token)
    return session_response(request, user)


@auth_router.get("/callback/google", name="auth_google_callback")
async def auth_google_callback(request: Request) -> Response:
    """Handle Google OAuth callback, issue a JWT, and redirect home."""

    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as exc:  # pragma: no cover - network dependency
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail("oauth_error", f"Google login failed: {exc.error}"),
        ) from exc

    userinfo = token.get("userinfo") if isinstance(token, dict) else None
    if not userinfo:
        try:
            userinfo = await oauth.google.parse_id_token(request, token)
        except Exception as exc:  # pragma: no cover - network dependency
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("oauth_error", f"Unable to parse Google profile: {exc}"),
            ) from exc

    provider_account_id = (userinfo or {}).get("sub")
    if not provider_account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail("oauth_error", "Google response missing subject claim."),
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


@auth_router.get("/logout")
def auth_logout(request: Request) -> Response:
    """Clear session cookies and return to the dashboard shell."""

    response = RedirectResponse(url="/")
    _clear_auth_cookie(response)
    try:
        request.session.clear()
    except Exception:  # pragma: no cover - session backend specific
        pass
    return response


__all__ = [
    "auth_router",
    "require_authenticated_user",
    "session_response",
    "_resolve_user_from_cookie",
]
