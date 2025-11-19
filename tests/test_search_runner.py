from datetime import datetime, timezone

from src.jobs import search_runner
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

    persisted = search_runner._persisted_report_from(report, job_id=123)

    assert persisted.required_tools == ["zapier", "slack", "make", "airtable"]
