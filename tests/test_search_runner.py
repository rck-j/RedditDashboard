from datetime import datetime, timezone
import logging
from datetime import datetime, timezone
import logging
from types import SimpleNamespace

import pytest
from prawcore.exceptions import TooManyRequests
from sqlmodel import SQLModel, Session, create_engine, select

from src.jobs import search_runner
from src.db.models import (
    AuthProvider,
    JobStatus,
    PersistedPostReport,
    SearchJob,
    User,
)
from src.services.analyzer import AutomationInsight, InitialAssessment, PostReport


def _report(required_tools: list[str]) -> PostReport:
    created = datetime.now(timezone.utc)
    return PostReport(
        submission_id="abc",
        subreddit="test",
        title="Example",
        url="https://reddit.com",
        permalink="/r/test/abc",
        created=created.isoformat(),
        created_utc=created,
        score=1,
        num_comments=0,
        initial_assessment=InitialAssessment(is_automation=True, rationale="yes"),
        automation_insight=AutomationInsight(
            automation_summary="summary",
            deep_analysis="analysis",
            automation_complexity="high",
            required_tools=required_tools,
        ),
    )


def test_persisted_report_normalizes_required_tools() -> None:
    report = _report([
        "Zapier, Slack",
        " zapier ",
        "Make / Airtable",
        "",
    ])

    job = SearchJob(
        id=123,
        user_id=1,
        subreddits=["test"],
        query="automation",
        time_filter="day",
        limit=5,
        comments_limit=1,
    )

    persisted = search_runner._persisted_report_from(report, job)

    assert persisted.required_tools == ["zapier", "slack", "make", "airtable"]


def test_run_records_job_error_and_keeps_partial_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog
) -> None:
    database_path = tmp_path / "worker.db"
    engine = create_engine(
        f"sqlite:///{database_path}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)

    def _get_session() -> Session:
        return Session(engine)

    monkeypatch.setattr(search_runner, "get_session", _get_session, raising=False)
    monkeypatch.setattr(search_runner, "purge_expired_jobs", lambda: None)
    monkeypatch.setattr(search_runner, "build_reddit_client", lambda: object())
    monkeypatch.setattr(
        search_runner, "build_openai_client", lambda: (object(), "test-model")
    )

    class _FakeAnalyzer:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def analyze_post(self, *_args, **_kwargs) -> PostReport:
            return _report(["zapier"])

    monkeypatch.setattr(search_runner, "AutomationAnalyzer", _FakeAnalyzer)

    def _response() -> SimpleNamespace:
        return SimpleNamespace(status_code=429, headers={}, text="rate limit")

    def _failing_search_posts(*_args, **_kwargs):
        yield {"id": "abc"}
        raise TooManyRequests(_response())

    monkeypatch.setattr(search_runner, "search_posts", _failing_search_posts)

    with _get_session() as session:
        user = User(
            auth_provider=AuthProvider.SYSTEM,
            provider_account_id="runner-test",
            email="runner@example.com",
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        job = SearchJob(
            user_id=user.id,
            subreddits=["test"],
            query="automation",
            time_filter="day",
            limit=5,
            comments_limit=1,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    caplog.set_level(logging.ERROR)
    with pytest.raises(TooManyRequests):
        search_runner.run(
            search_job_id=job_id,
            subreddits=["test"],
            query="automation",
            time_filter="day",
            limit=5,
            comments_limit=1,
        )

    assert "failed (rate_limited)" in caplog.text

    with _get_session() as session:
        stored_job = session.get(SearchJob, job_id)
        assert stored_job.status == JobStatus.FAILED
        assert stored_job.error_detail is not None
        assert stored_job.error_detail["code"] == "rate_limited"
        assert stored_job.processed_count == 1
        reports = list(
            session.exec(
                select(PersistedPostReport).where(
                    PersistedPostReport.search_job_id == job_id
                )
            )
        )
        assert len(reports) == 1
