"""Persistence helpers for dashboard User records."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlmodel import Session, select

from src.db.models import AuthProvider, SubscriptionPlan, User


class UserNotFoundError(RuntimeError):
    """Raised when a requested User cannot be located."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_user_by_id(session: Session, user_id: int) -> User | None:
    """Fetch a user by primary key."""

    return session.get(User, user_id)


def get_user_by_provider_identity(
    session: Session,
    *,
    provider: AuthProvider,
    provider_account_id: str,
) -> User | None:
    """Return a user matching the provider/account tuple."""

    stmt = select(User).where(
        User.auth_provider == provider,
        User.provider_account_id == provider_account_id,
    )
    return session.exec(stmt).first()


def upsert_user_from_identity(
    session: Session,
    *,
    provider: AuthProvider,
    provider_account_id: str,
    email: str | None = None,
    display_name: str | None = None,
    avatar_url: str | None = None,
    subscription_plan: SubscriptionPlan | None = None,
    is_active: bool = True,
    track_login: bool = True,
) -> User:
    """Insert or update a user record using OAuth identity claims."""

    user = get_user_by_provider_identity(
        session,
        provider=provider,
        provider_account_id=provider_account_id,
    )
    now = _utcnow()
    if user is None:
        user = User(
            auth_provider=provider,
            provider_account_id=provider_account_id,
            email=email,
            display_name=display_name,
            avatar_url=avatar_url,
            subscription_plan=subscription_plan or SubscriptionPlan.FREE,
            is_active=is_active,
            last_login_at=now if track_login else None,
        )
        session.add(user)
        session.flush()
        return user

    updated = False
    if email is not None and user.email != email:
        user.email = email
        updated = True
    if display_name is not None and user.display_name != display_name:
        user.display_name = display_name
        updated = True
    if avatar_url is not None and user.avatar_url != avatar_url:
        user.avatar_url = avatar_url
        updated = True
    if subscription_plan is not None and user.subscription_plan != subscription_plan:
        user.subscription_plan = subscription_plan
        updated = True
    if user.is_active != is_active:
        user.is_active = is_active
        updated = True
    if track_login:
        user.last_login_at = now
        updated = True
    if updated:
        user.updated_at = now
    session.add(user)
    session.flush()
    return user


def deactivate_user(session: Session, user_id: int) -> User:
    """Disable a user account without deleting the row."""

    user = session.get(User, user_id)
    if user is None:
        raise UserNotFoundError(f"User {user_id} not found")
    if not user.is_active:
        return user
    user.is_active = False
    user.updated_at = _utcnow()
    session.add(user)
    session.flush()
    return user


def ensure_system_user(session: Session) -> User:
    """Create or fetch the built-in system user for unauthenticated flows."""

    return upsert_user_from_identity(
        session,
        provider=AuthProvider.SYSTEM,
        provider_account_id="system",
        email="system@reddash.local",
        display_name="System",
        track_login=False,
    )


__all__ = [
    "UserNotFoundError",
    "deactivate_user",
    "ensure_system_user",
    "get_user_by_id",
    "get_user_by_provider_identity",
    "upsert_user_from_identity",
]
