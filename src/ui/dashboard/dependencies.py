"""Runtime dependencies shared across routers."""
from __future__ import annotations

from rq import Queue

from src.infra.redis import get_redis_client

redis_client = get_redis_client()
queue = Queue("reddit-searches", connection=redis_client)

__all__ = ["redis_client", "queue"]
