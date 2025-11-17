"""Helpers for caching search jobs by normalized Reddit parameters."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Iterable, List, Sequence

from redis import Redis

from src.db.models import JobStatus
from src.infra.search_jobs import SearchJob, get_search_job

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


def fetch_cached_job(
    redis_client: Redis,
    *,
    subreddits: Sequence[str],
    query: str,
    time_filter: str,
    limit: int,
    comments_limit: int,
) -> SearchJob | None:
    """Return a cached job for the provided parameters when available."""

    normalized = _normalize_payload(
        subreddits=subreddits,
        query=query,
        time_filter=time_filter,
        limit=limit,
        comments_limit=comments_limit,
    )
    cache_key = _cache_key(normalized)
    cached_job_id = redis_client.get(cache_key)
    if not cached_job_id:
        return None
    try:
        job_id = int(cached_job_id)
    except (TypeError, ValueError):
        redis_client.delete(cache_key)
        return None

    try:
        job = get_search_job(job_id)
    except RuntimeError:
        redis_client.delete(cache_key)
        return None

    if job.status == JobStatus.FAILED:
        redis_client.delete(cache_key)
        return None

    return job


def remember_search_job(
    redis_client: Redis,
    *,
    job_id: int,
    subreddits: Sequence[str],
    query: str,
    time_filter: str,
    limit: int,
    comments_limit: int,
) -> None:
    """Persist job metadata in Redis so duplicate requests can be skipped."""

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
    redis_client.setex(cache_key, ttl, str(job_id))


__all__ = [
    "fetch_cached_job",
    "remember_search_job",
]
