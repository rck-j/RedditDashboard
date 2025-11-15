"""Shared Redis client helpers."""

from __future__ import annotations

import os
from functools import lru_cache

import redis
from dotenv import load_dotenv

load_dotenv()

DEFAULT_URL = "redis://localhost:6379/0"


@lru_cache(maxsize=1)
def get_redis_client() -> redis.Redis:
    """Return a cached Redis client configured via REDIS_URL."""

    redis_url = os.getenv("REDIS_URL", DEFAULT_URL)
    return redis.from_url(redis_url)


__all__ = ["get_redis_client"]
