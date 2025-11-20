"""Repository helpers for SQLModel persistence."""

from .user_repo import (
    UserNotFoundError,
    deactivate_user,
    ensure_system_user,
    get_user_by_id,
    get_user_by_provider_identity,
    upsert_user_from_identity,
)

__all__ = [
    "UserNotFoundError",
    "deactivate_user",
    "ensure_system_user",
    "get_user_by_id",
    "get_user_by_provider_identity",
    "upsert_user_from_identity",
]
