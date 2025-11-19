"""Environment helpers for CLI and FastAPI entrypoints."""

from __future__ import annotations

import os
from typing import Iterable, Sequence

from dotenv import load_dotenv

load_dotenv()

REQUIRED_SECRETS: Sequence[str] = (
    "OPENAI_API_KEY",
    "PRAW_CLIENT_ID",
    "PRAW_CLIENT_SECRET",
    "PRAW_USER_AGENT",
)

OPTIONAL_PRAW_LOGIN: Sequence[str] = (
    "PRAW_USERNAME",
    "PRAW_PASSWORD",
)


def _require_env(key: str) -> str:
    """Return an environment variable or raise a descriptive error."""

    value = os.getenv(key)
    if not value:
        raise RuntimeError(f"Missing {key}; define it in .env or your shell.")
    return value


def ensure_required_secrets(keys: Iterable[str] | None = None) -> None:
    """Validate the configured secrets before the app continues to start."""

    required_keys = list(keys) if keys is not None else list(REQUIRED_SECRETS)
    missing = []
    for key in required_keys:
        try:
            _require_env(key)
        except RuntimeError:
            missing.append(key)
    if missing:
        missing_list = ", ".join(missing)
        raise RuntimeError(
            "Missing required environment variables: "
            f"{missing_list}. Populate them in .env or export them before running RedDash."
        )


__all__ = ["_require_env", "ensure_required_secrets", "REQUIRED_SECRETS", "OPTIONAL_PRAW_LOGIN"]
