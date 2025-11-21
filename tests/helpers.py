from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable
import threading
import time

from sqlmodel import SQLModel, Session, create_engine

from src.db import session as db_session
from src.infra import search_jobs
from src.jobs import search_runner
from src.services.analyzer import AutomationInsight, InitialAssessment, PostReport
from src.ui import report_dashboard


class FakeRedis:
    """Minimal in-memory Redis replacement for tests."""

    def __init__(self) -> None:
        self._store: dict[str, int] = {}

    def incr(self, key: str) -> int:
        value = self._store.get(key, 0) + 1
        self._store[key] = value
        return value

    def expire(self, key: str, _ttl: int) -> None:
        # Expiry tracking isn't required for deterministic tests.
        return None


@dataclass
class EnqueuedJob:
    func: Callable
    args: tuple
    kwargs: dict


QUEUE_ONLY_KWARGS = {"job_timeout"}


class InMemoryQueue:
    """RQ-like queue that stores jobs for deterministic execution."""

    def __init__(self, *, auto_run: bool = False) -> None:
        self.auto_run = auto_run
        self.jobs: list[EnqueuedJob] = []
        self._threads: list[threading.Thread] = []

    def enqueue(self, func: Callable, *args, **kwargs):  # pragma: no cover - signature parity
        call_kwargs = {k: v for k, v in kwargs.items() if k not in QUEUE_ONLY_KWARGS}
        job = EnqueuedJob(func=func, args=args, kwargs=call_kwargs)
        self.jobs.append(job)
        if self.auto_run:
            thread = threading.Thread(
                target=func,
                args=args,
                kwargs=kwargs,
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        return job

    def run_next(self) -> str | None:
        if not self.jobs:
            raise RuntimeError("No jobs have been enqueued.")
        job = self.jobs.pop(0)
        return job.func(*job.args, **job.kwargs)

    def __len__(self) -> int:
        return len(self.jobs)

    def join_all(self, timeout: float = 5.0) -> None:
        for thread in list(self._threads):
            thread.join(timeout)
        self._threads.clear()


@dataclass
class AppTestContext:
    queue: InMemoryQueue
    session_factory: Callable[[], Session]

    def run_enqueued_job(self) -> str | None:
        return self.queue.run_next()

    def new_session(self) -> Session:
        return self.session_factory()


def _build_post_report(summary: dict) -> PostReport:
    created_dt = summary.get("created_dt") or datetime(2024, 1, 1, tzinfo=timezone.utc)
    created_text = summary.get("created") or created_dt.isoformat()
    return PostReport(
        submission_id=summary.get("id", "post"),
        subreddit=summary.get("subreddit", "test"),
        title=summary.get("title", "Example post"),
        url=summary.get("url", "https://reddit.com/example"),
        permalink=summary.get("permalink", "/r/test/example"),
        created=created_text,
        created_utc=created_dt,
        score=summary.get("score", 1),
        num_comments=summary.get("num_comments", 0),
        initial_assessment=InitialAssessment(
            is_automation=summary.get("is_automation", True),
            rationale=summary.get(
                "initial_rationale",
                f"Automation rationale for {summary.get('id', 'post')}.",
            ),
        ),
        automation_insight=AutomationInsight(
            automation_summary=summary.get(
                "summary", f"Summary for {summary.get('id', 'post')}"
            ),
            deep_analysis=summary.get("analysis", "Detailed automation insight."),
            automation_complexity=summary.get("complexity", "medium"),
            required_tools=list(summary.get("tools", ["zapier"])),
        ),
    )


def configure_test_app(
    monkeypatch,
    tmp_path,
    *,
    auto_run_queue: bool = False,
    analysis_delay: float = 0.0,
) -> AppTestContext:
    """Wire up the FastAPI app and worker for deterministic tests."""

    database_path = tmp_path / "search_jobs.db"
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
    monkeypatch.setattr(search_runner, "get_session", _get_session, raising=False)
    monkeypatch.setattr(search_jobs, "get_session", _get_session, raising=False)

    fake_redis = FakeRedis()
    monkeypatch.setattr(report_dashboard, "redis_client", fake_redis, raising=False)
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
    monkeypatch.setattr(report_dashboard, "set_queue_depth", lambda *args, **kwargs: None)

    queue = InMemoryQueue(auto_run=auto_run_queue)
    monkeypatch.setattr(report_dashboard, "queue", queue, raising=False)

    monkeypatch.setattr(search_runner, "purge_expired_jobs_by_plan", lambda *args, **kwargs: {})
    monkeypatch.setattr(search_runner, "build_reddit_client", lambda: object())
    monkeypatch.setattr(
        search_runner, "build_openai_client", lambda: (object(), "test-model")
    )

    def _search_posts_default(*_args, **_kwargs) -> Iterable[dict]:
        return []

    monkeypatch.setattr(search_runner, "search_posts", _search_posts_default)

    class _StubAutomationAnalyzer:
        def __init__(self, *_args, **_kwargs) -> None:
            return None

        def analyze_post(self, summary, *_args, **_kwargs) -> PostReport:
            if analysis_delay:
                time.sleep(analysis_delay)
            return _build_post_report(summary)

    monkeypatch.setattr(search_runner, "AutomationAnalyzer", _StubAutomationAnalyzer)

    return AppTestContext(queue=queue, session_factory=_get_session)
