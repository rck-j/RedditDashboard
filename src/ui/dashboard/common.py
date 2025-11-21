"""Shared helpers for dashboard routers."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict

from fastapi import HTTPException, Request, status
from redis.exceptions import RedisError

from src.ui.dashboard import settings
from src.ui.dashboard.dependencies import redis_client


def error_detail(code: str, message: str, *, field: str | None = None) -> Dict[str, str]:
    payload = {"code": code, "message": message}
    if field:
        payload["field"] = field
    return payload


def enforce_rate_limit(request: Request) -> None:
    """Naive per-IP rate limiting backed by Redis."""

    identifier = request.client.host if request.client else "anonymous"
    window = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    key = f"rate-limit:{identifier}:{window}"
    try:
        hits = redis_client.incr(key)
        if hits == 1:
            redis_client.expire(key, 60)
        if hits > settings.RATE_LIMIT_REQUESTS_PER_MINUTE:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=error_detail(
                    "rate_limit_exceeded",
                    "Too many requests; please wait a moment before retrying.",
                ),
            )
    except RedisError:  # pragma: no cover - network-dependent
        return


def wants_html_response(request: Request) -> bool:
    """Detect whether the client expects an HTML navigation flow."""

    accept_header = (request.headers.get("accept") or "").lower()
    content_type = (request.headers.get("content-type") or "").lower()
    return "text/html" in accept_header or "application/x-www-form-urlencoded" in content_type
