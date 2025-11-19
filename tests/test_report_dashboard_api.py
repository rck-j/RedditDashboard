from __future__ import annotations

from typing import Dict, List, Tuple

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine, select

from src import config as app_config
from src.db.models import JobStatus, PersistedPostReport, SearchJob
from src.db import session as db_session
from src.ui import report_dashboard


@pytest.fixture()
def api_client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Tuple[TestClient, List[Dict]]:
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
    monkeypatch.setattr(report_dashboard, "fetch_cached_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(report_dashboard, "remember_search_job", lambda *args, **kwargs: None)

    enqueue_calls: List[Dict] = []

    def _fake_enqueue(*args, **kwargs) -> None:
        enqueue_calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(report_dashboard.queue, "enqueue", _fake_enqueue)

    client = TestClient(report_dashboard.app)
    yield client, enqueue_calls

    enqueue_calls.clear()


def _create_job(**overrides) -> SearchJob:
    job = SearchJob(
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
    report = PersistedPostReport(
        search_job_id=job_id,
        submission_id=overrides.pop("submission_id", "abc123"),
        subreddit=overrides.pop("subreddit", "test"),
        title=overrides.pop("title", "Example"),
        url=overrides.pop("url", "https://reddit.com/example"),
        permalink=overrides.pop("permalink", "/r/test/example"),
        created=overrides.pop("created", "2023-09-01"),
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
    client, _ = api_client
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
    client, _ = api_client

    response = client.get("/api/reports")

    assert response.status_code == 200
    assert response.json() == []
    assert response.headers["X-RedDash-Report-Status"] == "no-completed-job"
    assert (
        response.headers["X-RedDash-Report-Message"]
        == "No completed search jobs yet. Launch one via POST /api/searches."
    )


def test_create_search_enqueues_job(api_client) -> None:
    client, enqueue_calls = api_client

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
    assert body["stats"]["processed_count"] == 0
    assert body["stats"]["top_subreddits"] is None
    assert body["stats"]["top_keywords"] is None
    assert enqueue_calls and enqueue_calls[0]["args"][0] == report_dashboard.search_runner.run


def test_create_search_validation_error(api_client) -> None:
    client, _ = api_client

    response = client.post(
        "/api/searches",
        json={"subreddits": [], "query": "", "limit": 0},
    )

    assert response.status_code == 422


def test_read_search_returns_reports(api_client) -> None:
    client, _ = api_client
    job = _create_job(status=JobStatus.SUCCEEDED, processed_count=1, total_count=1)
    _create_report(job.id)

    response = client.get(f"/api/searches/{job.id}")

    assert response.status_code == 200
    payload = response.json()
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
    top_subreddits = payload["stats"]["top_subreddits"]
    assert top_subreddits == [{"subreddit": "test", "count": 1}]
    top_keywords = payload["stats"]["top_keywords"]
    assert top_keywords[0]["keyword"] == "example"
    assert top_keywords[0]["count"] == 1
    tools = payload["stats"]["tools"]
    assert tools == [{"label": "tool", "count": 1}]
    assert len(payload["reports"]) == 1


def test_read_search_missing_job(api_client) -> None:
    client, _ = api_client

    response = client.get("/api/searches/999")

    assert response.status_code == 404


def test_list_searches_supports_filters(api_client) -> None:
    client, _ = api_client
    succeeded = _create_job(status=JobStatus.SUCCEEDED, query="automation")
    _create_job(status=JobStatus.RUNNING, query="other")
    _create_job(status=JobStatus.SUCCEEDED, query="automation", is_deleted=True)

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


def test_delete_search_marks_deleted_and_clears_reports(api_client) -> None:
    client, _ = api_client
    job = _create_job(status=JobStatus.SUCCEEDED)
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


def test_list_searches_invalid_order(api_client) -> None:
    client, _ = api_client

    response = client.get("/api/searches", params={"order": "sideways"})

    assert response.status_code == 422


def test_get_app_config_returns_metadata(api_client) -> None:
    client, _ = api_client

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
