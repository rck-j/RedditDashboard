from __future__ import annotations

from typing import Any, Dict

import pytest

from src.db.models import JobStatus, SearchJob
from src.infra import search_cache


class FakeRedis:
    def __init__(self) -> None:
        self.storage: Dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.storage.get(key)

    def setex(self, key: str, ttl: int, value: Any) -> None:  # pragma: no cover - stub
        self.storage[key] = str(value).encode("utf-8")

    def delete(self, key: str) -> None:
        self.storage.pop(key, None)


@pytest.fixture()
def fake_job() -> SearchJob:
    return SearchJob(
        id=42,
        subreddits=["test"],
        query="automation",
        time_filter="month",
        limit=5,
        comments_limit=2,
        status=JobStatus.SUCCEEDED,
    )


def test_search_cache_round_trip(monkeypatch: pytest.MonkeyPatch, fake_job: SearchJob) -> None:
    redis_client = FakeRedis()

    def _fake_get_search_job(job_id: int) -> SearchJob:
        assert job_id == 42
        return fake_job

    monkeypatch.setenv("SEARCH_CACHE_TTL_SECONDS", "10")
    monkeypatch.setattr(search_cache, "get_search_job", _fake_get_search_job)

    search_cache.remember_search_job(
        redis_client,
        job_id=42,
        subreddits=["test"],
        query="automation",
        time_filter="month",
        limit=5,
        comments_limit=2,
    )

    cached_job = search_cache.fetch_cached_job(
        redis_client,
        subreddits=["test"],
        query="automation",
        time_filter="month",
        limit=5,
        comments_limit=2,
    )

    assert cached_job is fake_job
