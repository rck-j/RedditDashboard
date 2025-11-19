from datetime import datetime, timezone, timedelta

import pytest

from src.db.models import PersistedPostReport
from src.services.analytics import calculate_complexity_distribution


def _report(**overrides) -> PersistedPostReport:
    created_at = overrides.pop("created_at", datetime(2024, 1, 1, tzinfo=timezone.utc))
    return PersistedPostReport(
        search_job_id=overrides.pop("search_job_id", 1),
        submission_id=overrides.pop("submission_id", "abc"),
        subreddit=overrides.pop("subreddit", "test"),
        title=overrides.pop("title", "Example"),
        url=overrides.pop("url", "https://reddit.com/example"),
        permalink=overrides.pop("permalink", "/r/test/example"),
        created=overrides.pop("created", "2024-01-01"),
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
