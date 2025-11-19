"""Helpers for caching search jobs by normalized Reddit parameters."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Iterable, List, Sequence

from pydantic import ValidationError
from redis import Redis

from src.api.schemas import SearchJobResponse
from src.db.models import JobStatus
from src.infra.search_jobs import get_search_job

CACHE_PREFIX = "search-jobs"
DEFAULT_CACHE_TTL_SECONDS = 3600


def _ttl_seconds() -> int:
    """Return the configured TTL (seconds) for cached search jobs."""

    raw_value = os.getenv("SEARCH_CACHE_TTL_SECONDS")
    try:
        ttl = int(raw_value) if raw_value is not None else DEFAULT_CACHE_TTL_SECONDS
    except ValueError:
        ttl = DEFAULT_CACHE_TTL_SECONDS
    return max(ttl, 0)


def _canonicalize_subreddits(subreddits: Iterable[str]) -> List[str]:
    return sorted({sub.strip().lower() for sub in subreddits if sub.strip()})


def _normalize_payload(
    *,
    subreddits: Sequence[str],
    query: str,
    time_filter: str,
    limit: int,
    comments_limit: int,
) -> str:
    normalized = {
        "subreddits": _canonicalize_subreddits(subreddits),
        "query": " ".join(query.split()).lower(),
        "time_filter": time_filter.lower(),
        "limit": int(limit),
        "comments_limit": int(comments_limit),
    }
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def _cache_key(normalized_payload: str) -> str:
    digest = hashlib.sha256(normalized_payload.encode("utf-8")).hexdigest()
    return f"{CACHE_PREFIX}:{digest}"


def _decode_cached_value(blob: bytes | str | None) -> dict | None:
    if not blob:
        return None
    if isinstance(blob, bytes):
        try:
            blob = blob.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return None


def fetch_cached_job_response(
    redis_client: Redis,
    *,
    subreddits: Sequence[str],
    query: str,
    time_filter: str,
    limit: int,
    comments_limit: int,
) -> SearchJobResponse | None:
    """Return a cached job response when available and still valid."""

    normalized = _normalize_payload(
        subreddits=subreddits,
        query=query,
        time_filter=time_filter,
        limit=limit,
        comments_limit=comments_limit,
    )
    cache_key = _cache_key(normalized)
    cached_payload = _decode_cached_value(redis_client.get(cache_key))
    if not cached_payload:
        return None

    try:
        job_response = SearchJobResponse.model_validate(cached_payload)
    except ValidationError:
        redis_client.delete(cache_key)
        return None

    try:
        job = get_search_job(job_response.id)
    except RuntimeError:
        redis_client.delete(cache_key)
        return None

    if job.is_deleted or job.status == JobStatus.FAILED:
        redis_client.delete(cache_key)
        return None

    return job_response


def remember_search_job_response(
    redis_client: Redis,
    *,
    job_response: SearchJobResponse,
    subreddits: Sequence[str],
    query: str,
    time_filter: str,
    limit: int,
    comments_limit: int,
    include_reports: bool = False,
) -> None:
    """Persist serialized job metadata to Redis for duplicate detection."""

    ttl = _ttl_seconds()
    if ttl == 0:
        return

    normalized = _normalize_payload(
        subreddits=subreddits,
        query=query,
        time_filter=time_filter,
        limit=limit,
        comments_limit=comments_limit,
    )
    cache_key = _cache_key(normalized)
    payload = job_response
    if not include_reports:
        payload = job_response.model_copy(update={"reports": None})
    redis_client.setex(cache_key, ttl, payload.model_dump_json())


__all__ = [
    "fetch_cached_job_response",
    "remember_search_job_response",
]
