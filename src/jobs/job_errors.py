"""Structured error helpers for RQ-powered search jobs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

try:  # pragma: no cover - optional dependency guard
    from openai import OpenAIError
except Exception:  # pragma: no cover - fallback when OpenAI missing
    class OpenAIError(Exception):
        """Fallback OpenAI error used when the SDK is unavailable."""

        pass

try:  # pragma: no cover - optional dependency guard
    from praw.exceptions import RedditAPIException
except Exception:  # pragma: no cover
    class RedditAPIException(Exception):
        """Fallback Reddit API error for environments without PRAW."""

        error_type: str | None = None

        def __init__(self, message: str = "", error_type: str | None = None) -> None:
            super().__init__(message)
            self.error_type = error_type

try:  # pragma: no cover - optional dependency guard
    from prawcore.exceptions import Forbidden, TooManyRequests
except Exception:  # pragma: no cover
    class Forbidden(Exception):
        """Fallback forbidden error used when prawcore is missing."""

        pass

    class TooManyRequests(Exception):
        """Fallback rate limit error used when prawcore is missing."""

        pass


class JobError(BaseModel):
    """Structured description of a failed search job."""

    code: Literal[
        "rate_limited",
        "banned_subreddit",
        "api_error",
        "openai_error",
        "unknown",
    ]
    message: str
    retryable: bool = True
    context: dict[str, Any] | None = None


def job_error_from_exception(exc: Exception) -> JobError:
    """Map known API failures to a normalized ``JobError`` payload."""

    if isinstance(exc, TooManyRequests):
        return JobError(
            code="rate_limited",
            message="Reddit API rate limit exceeded. Please retry later.",
            retryable=True,
            context={"details": str(exc) or exc.__class__.__name__},
        )
    if isinstance(exc, Forbidden):
        return JobError(
            code="banned_subreddit",
            message="Reddit denied access to one or more subreddits.",
            retryable=False,
            context={"details": str(exc) or exc.__class__.__name__},
        )
    if isinstance(exc, RedditAPIException):
        return JobError(
            code="api_error",
            message="Encountered a Reddit API exception while searching.",
            retryable=True,
            context={
                "details": str(exc),
                "error_type": getattr(exc, "error_type", None),
            },
        )
    if isinstance(exc, OpenAIError):
        return JobError(
            code="openai_error",
            message="OpenAI API rejected or throttled the request.",
            retryable=True,
            context={"details": str(exc)},
        )
    return JobError(
        code="unknown",
        message=str(exc) or exc.__class__.__name__,
        retryable=True,
    )


__all__ = ["JobError", "job_error_from_exception"]
