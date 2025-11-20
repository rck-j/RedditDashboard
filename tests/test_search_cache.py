from __future__ import annotations

from typing import Any, Dict

from datetime import datetime, timezone

import pytest

from src.api.schemas import SearchJobResponse, SearchJobStats
from src.db.models import JobStatus, SearchJob
from src.infra import search_cache


class FakeRedis:
    def __init__(self) -> None:
        self.storage: Dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.storage.get(key)

    def setex(self, key: str, ttl: int, value: Any) -> None:  # pragma: no cover - stub
        encoded = value
        if not isinstance(encoded, (bytes, bytearray)):
            encoded = str(value).encode("utf-8")
        self.storage[key] = bytes(encoded)

    def delete(self, key: str) -> None:
        self.storage.pop(key, None)


@pytest.fixture()
def fake_job() -> SearchJob:
    return SearchJob(
        id=42,
        user_id=1,
        subreddits=["test"],
        query="automation",
        time_filter="month",
        limit=5,
        comments_limit=2,
        status=JobStatus.SUCCEEDED,
    )


@pytest.fixture()
def fake_job_response(fake_job: SearchJob) -> SearchJobResponse:
    stats = SearchJobStats(
        processed_count=1,
        total_count=1,
        average_score=10.0,
        report_count=1,
        summary=None,
        complexity=None,
        timeline=None,
        subreddits=None,
        keywords=None,
        tools=None,
    )
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return SearchJobResponse(
        id=fake_job.id,
        subreddits=fake_job.subreddits,
        query=fake_job.query,
        time_filter=fake_job.time_filter,
        limit=fake_job.limit,
        comments_limit=fake_job.comments_limit,
        status=fake_job.status,
        created_at=now,
        started_at=now,
        finished_at=now,
        has_partial_results=False,
        error_message=None,
        error=None,
        stats=stats,
        reports=None,
    )


def test_search_cache_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    fake_job: SearchJob,
    fake_job_response: SearchJobResponse,
) -> None:
    redis_client = FakeRedis()

    def _fake_get_search_job(job_id: int) -> SearchJob:
        assert job_id == fake_job.id
        return fake_job

    monkeypatch.setenv("SEARCH_CACHE_TTL_SECONDS", "10")
    monkeypatch.setattr(search_cache, "get_search_job", _fake_get_search_job)

    search_cache.remember_search_job_response(
        redis_client,
        job_response=fake_job_response,
        subreddits=fake_job.subreddits,
        query=fake_job.query,
        time_filter=fake_job.time_filter,
        limit=fake_job.limit,
        comments_limit=fake_job.comments_limit,
    )

    cached_job = search_cache.fetch_cached_job_response(
        redis_client,
        subreddits=fake_job.subreddits,
        query=fake_job.query,
        time_filter=fake_job.time_filter,
        limit=fake_job.limit,
        comments_limit=fake_job.comments_limit,
    )

    assert cached_job is not None
    assert cached_job.model_dump() == fake_job_response.model_dump()
