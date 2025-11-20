from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from prawcore.exceptions import TooManyRequests
from sqlmodel import select

from src.db.models import JobStatus, PersistedPostReport, SearchJob
from src.jobs import search_runner
from src.ui import report_dashboard

from tests.helpers import AppTestContext, configure_test_app


@pytest.fixture()
def job_api_client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> tuple[TestClient, AppTestContext]:
    context = configure_test_app(monkeypatch, tmp_path)
    client = TestClient(report_dashboard.app)
    yield client, context


def _sample_post(idx: int, **overrides) -> dict:
    created = datetime(2024, 1, idx + 1, tzinfo=timezone.utc)
    return {
        "id": f"post-{idx}",
        "subreddit": overrides.get("subreddit", "agents"),
        "title": overrides.get("title", f"Example {idx}"),
        "url": overrides.get("url", f"https://reddit.com/{idx}"),
        "permalink": overrides.get("permalink", f"/r/agents/{idx}"),
        "created_dt": overrides.get("created_dt", created),
        "score": overrides.get("score", idx + 1),
        "num_comments": overrides.get("num_comments", idx),
        "complexity": overrides.get("complexity", "medium"),
        "tools": overrides.get("tools", ["zapier"]),
        "is_automation": overrides.get("is_automation", True),
    }


def test_search_lifecycle_success(job_api_client, monkeypatch) -> None:
    client, context = job_api_client

    posts = [
        _sample_post(1, subreddit="agents", complexity="high", tools=["zapier", "slack"]),
        _sample_post(2, subreddit="automation", complexity="medium", tools=["notion"]),
    ]

    def _search_posts(*_args, **_kwargs):
        for post in posts:
            yield post

    monkeypatch.setattr(search_runner, "search_posts", _search_posts)

    response = client.post(
        "/api/searches",
        json={
            "subreddits": ["agents", "automation"],
            "query": "automation playbook",
            "time_filter": "month",
            "limit": 10,
            "comments_limit": 3,
        },
    )

    assert response.status_code == 201
    job_payload = response.json()
    job_id = job_payload["id"]
    assert job_payload["status"] == JobStatus.QUEUED.value
    assert len(context.queue.jobs) == 1

    context.run_enqueued_job()

    detail = client.get(f"/api/searches/{job_id}")
    assert detail.status_code == 200
    detail_payload = detail.json()
    assert detail_payload["status"] == JobStatus.SUCCEEDED.value
    assert detail_payload["stats"]["report_count"] == 2
    assert len(detail_payload["reports"]) == 2
    complexities = {
        report["automation_complexity"] for report in detail_payload["reports"]
    }
    assert complexities == {"high", "medium"}

    listing = client.get("/api/searches")
    assert listing.status_code == 200
    listing_payload = listing.json()
    assert listing_payload["total"] == 1
    assert listing_payload["items"][0]["status"] == JobStatus.SUCCEEDED.value
    assert listing_payload["items"][0]["stats"]["report_count"] == 2


def test_search_lifecycle_failed_job_preserves_partial_results(job_api_client, monkeypatch) -> None:
    client, context = job_api_client

    def _failing_search_posts(*_args, **_kwargs):
        yield _sample_post(1)
        raise TooManyRequests(SimpleNamespace(status_code=429, headers={}, text="rate limited"))

    monkeypatch.setattr(search_runner, "search_posts", _failing_search_posts)

    response = client.post(
        "/api/searches",
        json={
            "subreddits": ["agents"],
            "query": "automation failure",
            "time_filter": "month",
            "limit": 5,
            "comments_limit": 1,
        },
    )
    assert response.status_code == 201
    job_id = response.json()["id"]

    with pytest.raises(TooManyRequests):
        context.run_enqueued_job()

    detail = client.get(f"/api/searches/{job_id}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["status"] == JobStatus.FAILED.value
    assert payload["has_partial_results"] is True
    assert payload["error"]["code"] == "rate_limited"
    assert payload["stats"]["processed_count"] == 1
    assert payload["reports"] is None

    listing = client.get("/api/searches")
    assert listing.status_code == 200
    list_payload = listing.json()
    assert list_payload["items"][0]["status"] == JobStatus.FAILED.value
    assert list_payload["items"][0]["has_partial_results"] is True

    with context.new_session() as session:
        stored_job = session.get(SearchJob, job_id)
        assert stored_job is not None and stored_job.processed_count == 1
        reports = list(
            session.exec(
                select(PersistedPostReport).where(
                    PersistedPostReport.search_job_id == job_id
                )
            )
        )
        assert len(reports) == 1
