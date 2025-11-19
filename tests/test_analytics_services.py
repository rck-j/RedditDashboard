from datetime import datetime, timezone, timedelta

import pytest

from sqlmodel import SQLModel, Session, create_engine

from src.db.models import PersistedPostReport
from src.services.analytics import (
    build_timeline,
    calculate_complexity_distribution,
    calculate_tool_frequencies,
    extract_top_keywords,
    fetch_top_subreddits,
)


def _report(**overrides) -> PersistedPostReport:
    created_at = overrides.pop("created_at", datetime(2024, 1, 1, tzinfo=timezone.utc))
    created_utc = overrides.pop("created_utc", created_at)
    return PersistedPostReport(
        search_job_id=overrides.pop("search_job_id", 1),
        submission_id=overrides.pop("submission_id", "abc"),
        subreddit=overrides.pop("subreddit", "test"),
        title=overrides.pop("title", "Example"),
        url=overrides.pop("url", "https://reddit.com/example"),
        permalink=overrides.pop("permalink", "/r/test/example"),
        created=overrides.pop("created", "2024-01-01"),
        created_utc=created_utc,
        score=overrides.pop("score", 1),
        num_comments=overrides.pop("num_comments", 0),
        automation_complexity=overrides.pop("automation_complexity", "medium"),
        required_tools=overrides.pop("required_tools", []),
        insight_text=overrides.pop("insight_text", "insight"),
        created_at=created_at,
    )


def test_calculate_complexity_distribution_counts_and_percentages() -> None:
    reports = [
        _report(submission_id="1", automation_complexity="Low"),
        _report(submission_id="2", automation_complexity="medium"),
        _report(submission_id="3", automation_complexity="HIGH"),
        _report(submission_id="4", automation_complexity="unknown"),
        _report(submission_id="5", automation_complexity="  "),
    ]

    snapshot = calculate_complexity_distribution(reports)

    assert snapshot.total == 5
    assert snapshot.counts["low"] == 1
    assert snapshot.counts["medium"] == 1
    assert snapshot.counts["high"] == 1
    assert snapshot.counts["unknown"] == 2
    assert snapshot.percentages["low"] == pytest.approx(20.0)
    assert snapshot.percentages["unknown"] == pytest.approx(40.0)
    assert snapshot.timeline is None


def test_calculate_complexity_distribution_timeline() -> None:
    base_time = datetime(2024, 1, 1, tzinfo=timezone.utc)
    reports = [
        _report(submission_id="1", automation_complexity="low", created_at=base_time),
        _report(
            submission_id="2",
            automation_complexity="unknown",
            created_at=base_time + timedelta(hours=1),
        ),
        _report(
            submission_id="3",
            automation_complexity="medium",
            created_at=base_time + timedelta(days=1),
        ),
        _report(
            submission_id="4",
            automation_complexity="n/a",
            created_at=base_time + timedelta(days=1, hours=2),
        ),
    ]

    snapshot = calculate_complexity_distribution(reports, include_timeline=True)

    assert snapshot.timeline is not None
    assert snapshot.timeline[0].bucket == "2024-01-01"
    assert snapshot.timeline[0].automation_count == 1
    assert snapshot.timeline[0].non_automation_count == 1
    assert snapshot.timeline[1].bucket == "2024-01-02"
    assert snapshot.timeline[1].automation_count == 1
    assert snapshot.timeline[1].non_automation_count == 1


def test_build_timeline_supports_weekly_buckets() -> None:
    base_time = datetime(2024, 1, 3, tzinfo=timezone.utc)  # Wednesday
    reports = [
        _report(
            submission_id="1",
            automation_complexity="high",
            created_utc=base_time,
        ),
        _report(
            submission_id="2",
            automation_complexity="unknown",
            created_utc=base_time + timedelta(days=1),
        ),
        _report(
            submission_id="3",
            automation_complexity="medium",
            created_utc=base_time + timedelta(days=7),
        ),
    ]

    buckets = build_timeline(reports, bucket_size="weekly")

    assert len(buckets) == 2
    assert buckets[0].bucket_start.isoformat() == "2024-01-01T00:00:00+00:00"
    assert buckets[0].total_posts == 2
    assert buckets[0].automation_posts == 1
    assert buckets[1].bucket_start.isoformat() == "2024-01-08T00:00:00+00:00"
    assert buckets[1].total_posts == 1
    assert buckets[1].automation_posts == 1


def test_fetch_top_subreddits_returns_sorted_counts(tmp_path) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path/'analytics.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                _report(search_job_id=1, submission_id="1", subreddit="alpha"),
                _report(search_job_id=1, submission_id="2", subreddit="alpha"),
                _report(search_job_id=1, submission_id="3", subreddit="beta"),
                _report(search_job_id=2, submission_id="4", subreddit="gamma"),
            ]
        )
        session.commit()

        results = fetch_top_subreddits(session, job_id=1, limit=2)

        assert [entry.subreddit for entry in results] == ["alpha", "beta"]
        assert [entry.count for entry in results] == [2, 1]


def test_extract_top_keywords_filters_stop_words_and_sorts() -> None:
    reports = [
        _report(
            title="Agent automation agent",
            insight_text="Agents build automation tools for business",
        ),
        _report(title="Automation ideas", insight_text="agent agent support"),
    ]

    keywords = extract_top_keywords(reports, limit=3)

    assert [entry.keyword for entry in keywords] == ["agent", "automation", "agents"]
    assert [entry.count for entry in keywords] == [4, 3, 1]


def test_calculate_tool_frequencies_sorts_and_normalizes() -> None:
    reports = [
        _report(required_tools=["Zapier", "slack", "Zapier"]),
        _report(required_tools=["Make", "slack / email"]),
    ]

    stats = calculate_tool_frequencies(reports)

    assert [entry.label for entry in stats] == ["slack", "zapier", "email", "make"]
    assert [entry.count for entry in stats] == [2, 2, 1, 1]
