from __future__ import annotations

from typing import Dict, List, Tuple

import json
from datetime import datetime, timezone
from itertools import count

import pytest
from fastapi import Request, status
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine, select

from src import config as app_config
from src.api.schemas import SearchJobResponse, SearchJobStats
from src.db.models import (
    AuthProvider,
    JobStatus,
    PersistedPostReport,
    SearchJob,
    SubscriptionPlan,
    User,
)
from src.db import session as db_session
from src.ui import report_dashboard
from src.infra.cleanup import purge_expired_jobs_by_plan
from src.services.plans import archival_plans, get_plan_policy, plan_retention_days


@pytest.fixture()
def api_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> Tuple[TestClient, List[Dict], Dict[str, User]]:
    database_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{database_path}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)

    def _get_session() -> Session:
        return Session(engine)

    monkeypatch.setattr(db_session, "engine", engine, raising=False)
    monkeypatch.setattr(db_session, "get_session", _get_session, raising=False)
    monkeypatch.setattr(report_dashboard, "engine", engine, raising=False)
    monkeypatch.setattr(report_dashboard, "get_session", _get_session, raising=False)
    monkeypatch.setattr(
        report_dashboard,
        "fetch_cached_job_response",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        report_dashboard,
        "remember_search_job_response",
        lambda *args, **kwargs: None,
    )

    enqueue_calls: List[Dict] = []

    def _fake_enqueue(*args, **kwargs) -> None:
        enqueue_calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(report_dashboard.queue, "enqueue", _fake_enqueue)

    auth_context: Dict[str, User] = {"user": _create_user()}

    def _auth_dependency(request: Request) -> User:  # type: ignore[override]
        return auth_context["user"]

    report_dashboard.app.dependency_overrides[
        report_dashboard.require_authenticated_user
    ] = _auth_dependency

    client = TestClient(report_dashboard.app)
    yield client, enqueue_calls, auth_context

    enqueue_calls.clear()


_USER_SEQUENCE = count(1)


def _create_user(**overrides) -> User:
    identifier = next(_USER_SEQUENCE)
    user = User(
        auth_provider=overrides.pop("auth_provider", AuthProvider.SYSTEM),
        provider_account_id=overrides.pop(
            "provider_account_id", f"test-user-{identifier}"
        ),
        email=overrides.pop("email", f"user{identifier}@example.com"),
        display_name=overrides.pop("display_name", "Test User"),
        subscription_plan=overrides.pop(
            "subscription_plan", SubscriptionPlan.FREE
        ),
        **overrides,
    )
    with db_session.get_session() as session:
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def _create_job(**overrides) -> SearchJob:
    user_id = overrides.pop("user_id", None)
    if user_id is None:
        user_id = _create_user().id
    job = SearchJob(
        user_id=user_id,
        subreddits=overrides.pop("subreddits", ["test"]),
        query=overrides.pop("query", "automation"),
        time_filter=overrides.pop("time_filter", "month"),
        limit=overrides.pop("limit", 5),
        comments_limit=overrides.pop("comments_limit", 2),
        **overrides,
    )
    with db_session.get_session() as session:
        session.add(job)
        session.commit()
        session.refresh(job)
        return job


def _create_report(job_id: int, **overrides) -> PersistedPostReport:
    user_id = overrides.pop("user_id", None)
    if user_id is None:
        with db_session.get_session() as session:
            job = session.get(SearchJob, job_id)
            user_id = job.user_id if job else None
    report = PersistedPostReport(
        search_job_id=job_id,
        user_id=user_id,
        submission_id=overrides.pop("submission_id", "abc123"),
        subreddit=overrides.pop("subreddit", "test"),
        title=overrides.pop("title", "Example"),
        url=overrides.pop("url", "https://reddit.com/example"),
        permalink=overrides.pop("permalink", "/r/test/example"),
        created=overrides.pop("created", "2023-09-01"),
        created_utc=overrides.pop(
            "created_utc", datetime(2024, 1, 1, tzinfo=timezone.utc)
        ),
        score=overrides.pop("score", 10),
        num_comments=overrides.pop("num_comments", 2),
        automation_complexity=overrides.pop("automation_complexity", "medium"),
        required_tools=overrides.pop("required_tools", ["tool"]),
        insight_text=overrides.pop("insight_text", "Insight"),
        created_at=overrides.pop(
            "created_at", datetime(2024, 1, 1, tzinfo=timezone.utc)
        ),
    )
    with db_session.get_session() as session:
        session.add(report)
        session.commit()
        session.refresh(report)
        return report


def test_read_reports_returns_latest_job_payload(api_client) -> None:
    client, _, _ = api_client
    older_job = _create_job(
        status=JobStatus.SUCCEEDED,
        finished_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        query="old",
    )
    _create_report(older_job.id, submission_id="old")
    newer_job = _create_job(
        status=JobStatus.SUCCEEDED,
        finished_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
        query="latest",
    )
    _create_report(
        newer_job.id,
        submission_id="new",
        automation_complexity="high",
        insight_text="Deep analysis",  # ensures deterministic summary
    )

    response = client.get("/api/reports")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["submission_id"] == "new"
    assert payload[0]["automation_insight"]["automation_complexity"] == "high"
    stats_header = response.headers["X-RedDash-Report-Stats"]
    stats = json.loads(stats_header)
    assert stats["search_job_id"] == newer_job.id
    assert stats["report_count"] == 1


def test_read_reports_handles_missing_jobs(api_client) -> None:
    client, _, _ = api_client

    response = client.get("/api/reports")

    assert response.status_code == 200
    assert response.json() == []
    assert response.headers["X-RedDash-Report-Status"] == "no-completed-job"
    assert (
        response.headers["X-RedDash-Report-Message"]
        == "No completed search jobs yet. Launch one via POST /api/searches."
    )


def test_create_search_enqueues_job(api_client) -> None:
    client, enqueue_calls, _ = api_client

    response = client.post(
        "/api/searches",
        json={
            "subreddits": [" test "],
            "query": "  agents  ",
            "time_filter": "month",
            "limit": 10,
            "comments_limit": 1,
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["subreddits"] == ["test"]
    assert body["query"] == "agents"
    assert body["error"] is None
    assert body["has_partial_results"] is False
    assert body["stats"]["processed_count"] == 0
    assert body["stats"]["subreddits"] is None
    assert body["stats"]["keywords"] is None
    assert enqueue_calls and enqueue_calls[0]["args"][0] == report_dashboard.search_runner.run


def test_create_search_returns_cached_job(api_client, monkeypatch) -> None:
    client, enqueue_calls, _ = api_client
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
    cached_response = SearchJobResponse(
        id=99,
        subreddits=["test"],
        query="cached",
        time_filter="month",
        limit=10,
        comments_limit=1,
        status=JobStatus.SUCCEEDED,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        started_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        finished_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        error_message=None,
        error=None,
        has_partial_results=False,
        stats=stats,
        reports=None,
    )

    monkeypatch.setattr(
        report_dashboard,
        "fetch_cached_job_response",
        lambda *args, **kwargs: cached_response,
    )

    response = client.post(
        "/api/searches",
        json={
            "subreddits": ["test"],
            "query": "cached",
            "time_filter": "month",
            "limit": 10,
            "comments_limit": 1,
        },
    )

    assert response.status_code == 201
    assert response.json()["id"] == 99
    assert enqueue_calls == []


def test_create_search_validation_error(api_client) -> None:
    client, _, _ = api_client

    response = client.post(
        "/api/searches",
        json={"subreddits": [], "query": "", "limit": 0},
    )

    assert response.status_code == 422


def test_create_search_enforces_concurrent_limit(api_client, monkeypatch) -> None:
    client, enqueue_calls, auth_context = api_client
    monkeypatch.setattr(
        report_dashboard,
        "fetch_cached_job_response",
        lambda *args, **kwargs: None,
    )
    _create_job(user_id=auth_context["user"].id, status=JobStatus.RUNNING)

    response = client.post(
        "/api/searches",
        json={
            "subreddits": ["test"],
            "query": "automation",
            "time_filter": "month",
            "limit": 5,
            "comments_limit": 1,
        },
    )

    assert response.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    detail = response.json()["detail"]
    assert detail["code"] == "job_quota_exceeded"
    assert enqueue_calls == []


def test_create_search_daily_limit_respects_plan(api_client, monkeypatch) -> None:
    client, enqueue_calls, auth_context = api_client
    monkeypatch.setattr(
        report_dashboard,
        "fetch_cached_job_response",
        lambda *args, **kwargs: None,
    )
    limit = get_plan_policy(auth_context["user"].subscription_plan).daily_job_limit
    assert limit is not None

    for _ in range(limit):
        _create_job(user_id=auth_context["user"].id)

    response = client.post(
        "/api/searches",
        json={
            "subreddits": ["test"],
            "query": "automation",
            "time_filter": "month",
            "limit": 5,
            "comments_limit": 1,
        },
    )

    assert response.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    detail = response.json()["detail"]
    assert detail["code"] == "daily_quota_exceeded"
    assert enqueue_calls == []


def test_create_search_allows_independent_user_queues(api_client, monkeypatch) -> None:
    client, enqueue_calls, _ = api_client
    monkeypatch.setattr(
        report_dashboard,
        "fetch_cached_job_response",
        lambda *args, **kwargs: None,
    )
    other_user = _create_user()
    _create_job(user_id=other_user.id, status=JobStatus.RUNNING)

    response = client.post(
        "/api/searches",
        json={
            "subreddits": ["test"],
            "query": "automation",
            "time_filter": "month",
            "limit": 5,
            "comments_limit": 1,
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert enqueue_calls


def test_read_search_returns_reports(api_client) -> None:
    client, _, auth_context = api_client
    job = _create_job(
        user_id=auth_context["user"].id,
        status=JobStatus.SUCCEEDED,
        processed_count=1,
        total_count=1,
    )
    _create_report(job.id)

    response = client.get(f"/api/searches/{job.id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["error"] is None
    assert payload["has_partial_results"] is False
    assert payload["stats"]["report_count"] == 1
    summary = payload["stats"]["summary"]
    assert summary["total_posts"] == 1
    assert summary["automation_percentage"] == pytest.approx(100.0)
    assert summary["average_score"] == pytest.approx(10.0)
    assert summary["average_comment_count"] == pytest.approx(2.0)
    complexity = payload["stats"]["complexity"]
    assert complexity["total"] == 1
    assert complexity["counts"]["medium"] == 1
    assert complexity["percentages"]["medium"] == pytest.approx(100.0)
    assert complexity["timeline"][0]["automation_count"] == 1
    assert complexity["timeline"][0]["non_automation_count"] == 0
    subreddits = payload["stats"]["subreddits"]
    assert subreddits == [{"subreddit": "test", "count": 1}]
    top_keywords = payload["stats"]["keywords"]
    assert top_keywords[0]["keyword"] == "example"
    assert top_keywords[0]["count"] == 1
    tools = payload["stats"]["tools"]
    assert tools == [{"label": "tool", "count": 1}]
    timeline = payload["stats"]["timeline"]
    assert timeline["bucket_size"] == "daily"
    assert timeline["buckets"][0]["total_posts"] == 1
    assert timeline["buckets"][0]["automation_posts"] == 1
    assert len(payload["reports"]) == 1


def test_read_search_running_job_has_placeholder_stats(api_client) -> None:
    client, _, auth_context = api_client
    job = _create_job(
        user_id=auth_context["user"].id,
        status=JobStatus.RUNNING,
        processed_count=2,
        total_count=5,
    )
    _create_report(job.id)

    response = client.get(f"/api/searches/{job.id}")

    assert response.status_code == 200
    payload = response.json()
    stats = payload["stats"]
    assert stats["processed_count"] == 2
    assert stats["report_count"] == 0
    assert stats["summary"] is None
    assert stats["complexity"] is None
    assert stats["subreddits"] is None
    assert stats["keywords"] is None
    assert stats["tools"] is None
    assert stats["timeline"] is None
    assert payload["reports"] is None
    assert payload["has_partial_results"] is False


def test_read_search_failed_job_includes_error_payload(api_client) -> None:
    client, _, auth_context = api_client
    job = _create_job(
        user_id=auth_context["user"].id,
        status=JobStatus.FAILED,
        processed_count=3,
        total_count=5,
        error_message="Rate limit hit",
        error_detail={
            "code": "rate_limited",
            "message": "Rate limit hit",
            "retryable": True,
            "context": {"details": "rate limit"},
        },
    )

    response = client.get(f"/api/searches/{job.id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == JobStatus.FAILED.value
    assert payload["has_partial_results"] is True
    assert payload["error"]["code"] == "rate_limited"
    assert payload["error_message"] == "Rate limit hit"
    assert payload["stats"]["processed_count"] == 3


def test_read_search_rejects_other_user_job(api_client) -> None:
    client, _, _ = api_client
    other_job = _create_job()

    response = client.get(f"/api/searches/{other_job.id}")

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_read_search_missing_job(api_client) -> None:
    client, _, _ = api_client

    response = client.get("/api/searches/999")

    assert response.status_code == 404


def test_list_searches_supports_filters(api_client) -> None:
    client, _, auth_context = api_client
    succeeded = _create_job(
        user_id=auth_context["user"].id, status=JobStatus.SUCCEEDED, query="automation"
    )
    _create_job(user_id=auth_context["user"].id, status=JobStatus.RUNNING, query="other")
    _create_job(
        user_id=auth_context["user"].id,
        status=JobStatus.SUCCEEDED,
        query="automation",
        is_deleted=True,
    )

    response = client.get(
        "/api/searches",
        params={
            "status_filter": JobStatus.SUCCEEDED.value,
            "search": "automation",
            "page": 1,
            "page_size": 1,
            "order": "asc",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == succeeded.id


def test_list_searches_excludes_other_user_jobs(api_client) -> None:
    client, _, auth_context = api_client
    owned_job = _create_job(user_id=auth_context["user"].id)
    other_job = _create_job()

    response = client.get("/api/searches")

    assert response.status_code == 200
    payload = response.json()
    returned_ids = {item["id"] for item in payload["items"]}
    assert owned_job.id in returned_ids
    assert other_job.id not in returned_ids


def test_delete_search_marks_deleted_and_clears_reports(api_client) -> None:
    client, _, auth_context = api_client
    job = _create_job(user_id=auth_context["user"].id, status=JobStatus.SUCCEEDED)
    _create_report(job.id)

    delete_response = client.delete(f"/api/searches/{job.id}")
    assert delete_response.status_code == 204

    with db_session.get_session() as session:
        stored_job = session.get(SearchJob, job.id)
        assert stored_job.is_deleted
        reports = list(
            session.exec(
                select(PersistedPostReport).where(
                    PersistedPostReport.search_job_id == job.id
                )
            )
        )
        assert reports == []


def test_delete_search_rejects_other_user_job(api_client) -> None:
    client, _, _ = api_client
    other_job = _create_job()

    delete_response = client.delete(f"/api/searches/{other_job.id}")

    assert delete_response.status_code == status.HTTP_404_NOT_FOUND


def test_list_searches_invalid_order(api_client) -> None:
    client, _, _ = api_client

    response = client.get("/api/searches", params={"order": "sideways"})

    assert response.status_code == 422


def test_cleanup_respects_plan_retention_and_archival(api_client) -> None:
    _, _, _ = api_client
    old_timestamp = datetime(2020, 1, 1, tzinfo=timezone.utc)
    free_user = _create_user(subscription_plan=SubscriptionPlan.FREE)
    enterprise_user = _create_user(subscription_plan=SubscriptionPlan.ENTERPRISE)
    free_job = _create_job(
        user_id=free_user.id,
        created_at=old_timestamp,
        status=JobStatus.SUCCEEDED,
    )
    enterprise_job = _create_job(
        user_id=enterprise_user.id,
        created_at=old_timestamp,
        status=JobStatus.SUCCEEDED,
    )

    removed = purge_expired_jobs_by_plan(
        plan_retention_days(), archive_only_plans=archival_plans()
    )

    with db_session.get_session() as session:
        assert session.get(SearchJob, free_job.id) is None
        archived = session.get(SearchJob, enterprise_job.id)
        assert archived is not None
        assert archived.is_deleted is True
    assert removed[SubscriptionPlan.FREE] == 1
    assert removed[SubscriptionPlan.ENTERPRISE] == 1


def test_get_app_config_returns_metadata(api_client) -> None:
    client, _, _ = api_client

    response = client.get("/api/config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["time_filters"]["options"] == list(
        app_config.SEARCH_PARAMETERS.time_filters
    )
    assert (
        payload["limits"]["posts"]["default"]
        == app_config.SEARCH_PARAMETERS.limit_default
    )
    assert (
        payload["limits"]["posts"]["max"] == app_config.SEARCH_PARAMETERS.limit_max
    )
    assert (
        payload["limits"]["comments"]["max"]
        == app_config.SEARCH_PARAMETERS.comments_limit_max
    )
    prompts = app_config.get_prompts()
    assert payload["prompts"]["initial_assessment"] == prompts["initial_assessment"]
