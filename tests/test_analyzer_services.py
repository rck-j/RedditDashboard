"""Unit tests for the analyzer service helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import types
from typing import Any, Dict, Iterable, Iterator, List

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Provide lightweight stubs for optional runtime dependencies used by the CLI
# so the pure-function tests can import the module graph without extra packages.
fake_praw = types.ModuleType("praw")
fake_praw.Reddit = object  # type: ignore[assignment]
sys.modules.setdefault("praw", fake_praw)

fake_openai = types.ModuleType("openai")


class _FakeOpenAI:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.responses = types.SimpleNamespace(parse=lambda *a, **k: None)


fake_openai.OpenAI = _FakeOpenAI  # type: ignore[attr-defined]
sys.modules.setdefault("openai", fake_openai)

fake_dotenv = types.ModuleType("dotenv")
fake_dotenv.load_dotenv = lambda *a, **k: None  # type: ignore[assignment]
sys.modules.setdefault("dotenv", fake_dotenv)

from pydantic import BaseModel

from red import _save_report
from src.services.analyzer import (
    AnalysisReport,
    AutomationInsight,
    InitialAssessment,
    PostReport,
    _parse_as_model,
    _truncate,
    fetch_full_post,
    search_posts,
    summarize_post,
)


class DummySubmission:
    """Minimal Reddit submission stub for summarize/search tests."""

    def __init__(
        self,
        *,
        submission_id: str = "abc123",
        subreddit: str,
        title: str,
        permalink: str,
        created_utc: float,
        score: int,
        num_comments: int,
    ) -> None:
        self.id = submission_id
        self.subreddit = subreddit
        self.title = title
        self.permalink = permalink
        self.created_utc = created_utc
        self.score = score
        self.num_comments = num_comments


def test_truncate_short_strings_are_unchanged() -> None:
    assert _truncate("hi", 10) == "hi"


def test_truncate_long_strings_are_trimmed() -> None:
    assert _truncate("abcdef", 3) == "abc..."


def test_summarize_post_extracts_expected_fields() -> None:
    submission = DummySubmission(
        subreddit="automation",
        title="Need help",
        permalink="/r/automation/abc",
        created_utc=123.0,
        score=42,
        num_comments=3,
    )
    summary = summarize_post(submission)  # type: ignore[arg-type]
    assert summary == {
        "id": "abc123",
        "subreddit": "automation",
        "title": "Need help",
        "url": "https://www.reddit.com/r/automation/abc",
        "permalink": "/r/automation/abc",
        "created_utc": 123.0,
        "score": 42,
        "num_comments": 3,
    }


class DummySubreddit:
    def __init__(self, posts: Iterable[DummySubmission]):
        self._posts = list(posts)
        self.calls: List[Dict[str, Any]] = []

    def search(
        self, query: str, *, sort: str, time_filter: str, limit: int
    ) -> Iterator[DummySubmission]:
        self.calls.append(
            {"query": query, "sort": sort, "time_filter": time_filter, "limit": limit}
        )
        return iter(self._posts)


class DummyRedditClient:
    def __init__(self, subreddits: Dict[str, DummySubreddit]):
        self._subreddits = subreddits

    def subreddit(self, name: str) -> DummySubreddit:
        return self._subreddits[name]

    def submission(self, **kwargs: Any) -> "DummyFullSubmission":
        raise NotImplementedError


def test_search_posts_yields_summaries_from_each_subreddit() -> None:
    submissions = [
        DummySubmission(
            subreddit="a",
            title="First",
            permalink="/a1",
            created_utc=1,
            score=1,
            num_comments=0,
        ),
        DummySubmission(
            subreddit="b",
            title="Second",
            permalink="/a2",
            created_utc=2,
            score=2,
            num_comments=1,
        ),
    ]
    dummy_sub = DummySubreddit(submissions)
    reddit = DummyRedditClient({"testsub": dummy_sub})

    results = list(
        search_posts(
            reddit,  # type: ignore[arg-type]
            subreddits=["testsub"],
            query="automation",
            time_filter="month",
            limit=25,
        )
    )

    assert len(results) == 2
    assert results[0]["title"] == "First"
    assert dummy_sub.calls == [
        {"query": "automation", "sort": "new", "time_filter": "month", "limit": 25}
    ]


class DummyComment:
    def __init__(
        self,
        *,
        id: str,
        author: str | None,
        body: str,
        created_utc: float,
        score: int,
        parent_id: str,
    ) -> None:
        self.id = id
        self.author = author
        self.body = body
        self.created_utc = created_utc
        self.score = score
        self.parent_id = parent_id


class DummyCommentForest:
    def __init__(self, comments: List[DummyComment]):
        self._comments = comments
        self.replace_limit: int | None = None

    def replace_more(self, limit: int) -> None:
        self.replace_limit = limit

    def list(self) -> List[DummyComment]:
        return self._comments


class DummyFullSubmission:
    def __init__(self) -> None:
        self.id = "abc123"
        self.subreddit = "smallbusiness"
        self.title = "Need automation"
        self.selftext = "Body"
        self.author = "poster"
        self.created_utc = 111.0
        self.score = 5
        self.url = "https://example.com"
        self.is_self = True
        self.num_comments = 2
        self.media = None
        self.comments = DummyCommentForest(
            [
                DummyComment(
                    id="c1",
                    author="commenter",
                    body="First comment",
                    created_utc=222.0,
                    score=10,
                    parent_id="t3_abc123",
                ),
                DummyComment(
                    id="c2",
                    author=None,
                    body="Second comment",
                    created_utc=223.0,
                    score=3,
                    parent_id="t3_abc123",
                ),
            ]
        )


class DummyRedditWithSubmission(DummyRedditClient):
    def __init__(self, submission: DummyFullSubmission):
        super().__init__({})
        self._submission = submission
        self.calls: List[Dict[str, Any]] = []

    def submission(self, **kwargs: Any) -> DummyFullSubmission:
        self.calls.append(kwargs)
        return self._submission


def test_fetch_full_post_collects_submission_and_comments() -> None:
    submission = DummyFullSubmission()
    reddit = DummyRedditWithSubmission(submission)
    payload = fetch_full_post(reddit, "https://www.reddit.com/r/test", max_comments=1)

    assert payload["id"] == "abc123"
    assert payload["comments"][0]["body"] == "First comment"
    assert len(payload["comments"]) == 1
    assert submission.comments.replace_limit == 0
    assert reddit.calls == [{"url": "https://www.reddit.com/r/test"}]


class DemoModel(BaseModel):
    value: str


class FakeResponse:
    def __init__(self, parsed: DemoModel):
        self.parsed = parsed


class FakeResponses:
    def __init__(self, response: FakeResponse):
        self._response = response
        self.calls: List[Dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        return self._response


class FakeOpenAIClient:
    def __init__(self, response: FakeResponse):
        self.responses = FakeResponses(response)


def test_parse_as_model_returns_expected_pydantic_instance() -> None:
    model_instance = DemoModel(value="ok")
    fake_client = FakeOpenAIClient(FakeResponse(parsed=model_instance))

    result = _parse_as_model(
        fake_client,  # type: ignore[arg-type]
        model="demo",
        prompt="hi",
        text_format=DemoModel,
    )

    assert result == model_instance
    assert fake_client.responses.calls[0]["model"] == "demo"


def test_save_report_writes_schema_with_total_posts(tmp_path: Path) -> None:
    initial = InitialAssessment(is_automation=True, rationale="yes")
    insight = AutomationInsight(
        automation_summary="summary",
        deep_analysis="analysis",
        automation_complexity="low",
        required_tools=["tool"],
    )
    post = PostReport(
        submission_id="abc123",
        subreddit="test",
        title="title",
        url="https://reddit.com",
        permalink="/r/test/comments/abc123/title/",
        created="2024-01-01 00:00:00",
        created_utc=datetime(2024, 1, 1, tzinfo=timezone.utc),
        score=1,
        num_comments=0,
        initial_assessment=initial,
        automation_insight=insight,
    )
    report = AnalysisReport(
        generated_at="2024-01-01T00:00:00Z",
        query="automation",
        time_filter="month",
        posts=[post],
    )

    output = tmp_path / "report.json"
    _save_report(output, report)

    payload = json.loads(output.read_text())
    assert payload["total_posts"] == 1
    assert payload["posts"][0]["initial_assessment"]["is_automation"] is True
    assert payload["posts"][0]["automation_insight"]["automation_summary"] == "summary"
