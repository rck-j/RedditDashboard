"""Web dashboard for viewing Reddit automation reports."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError
from rq import Queue

from src.infra.redis import get_redis_client
from src.infra.search_jobs import SearchJob, create_search_job, set_rq_job_id
from src.jobs import search_runner

try:
    from red import PostReport as BasePostReport
except ImportError:  # pragma: no cover - fallback for standalone execution
    class InitialAssessment(BaseModel):
        """Structured output for the initial title-only screen."""

        is_automation: bool = Field(...)
        rationale: str = Field(...)

    class AutomationInsight(BaseModel):
        """Structured output for the full deep-dive analysis."""

        automation_summary: str
        deep_analysis: str
        automation_complexity: str
        required_tools: List[str] = Field(default_factory=list)

    class BasePostReport(BaseModel):
        subreddit: str
        title: str
        url: str
        created: str
        score: int
        num_comments: int
        initial_assessment: InitialAssessment
        automation_insight: AutomationInsight


class PostReport(BasePostReport):
    """Local alias to ensure consistent validation."""


class SearchRequest(BaseModel):
    """Request payload for launching a new Reddit search."""

    subreddits: List[str] = Field(
        default_factory=lambda: ["smallbusiness", "Entrepreneur"]
    )
    query: str = Field(
        default='(agent OR "ai agent" OR agentic OR automation) '
        "(small business OR smb OR entrepreneur)"
    )
    time_filter: str = Field(default="month")
    limit: int = Field(default=50)
    comments_limit: int = Field(default=5)


ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_PATH = ROOT_DIR / "data" / "report.json"
TEMPLATES = Jinja2Templates(directory=str(ROOT_DIR / "templates"))
ALLOWED_TIME_FILTERS = {"day", "week", "month", "year", "all"}

app = FastAPI(title="Reddit Automation Report Dashboard")
queue = Queue("reddit-searches", connection=get_redis_client())


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:  # pragma: no cover - runtime protection
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except json.JSONDecodeError as exc:  # pragma: no cover - runtime protection
        raise HTTPException(status_code=500, detail="Invalid report JSON") from exc


def _parse_reports(items: Iterable[Dict[str, Any]]) -> List[PostReport]:
    reports: List[PostReport] = []
    for item in items:
        try:
            reports.append(PostReport.model_validate(item))
        except ValidationError as exc:  # pragma: no cover - runtime protection
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    return reports


@lru_cache(maxsize=1)
def get_reports() -> List[PostReport]:
    """Load and parse report entries from disk."""

    payload = _load_json(DATA_PATH)
    posts = payload.get("posts")
    if posts is None:
        if isinstance(payload, list):
            posts = payload
        else:
            raise HTTPException(status_code=500, detail="Report payload missing 'posts'.")
    if not isinstance(posts, list):
        raise HTTPException(status_code=500, detail="Report posts should be a list.")
    return _parse_reports(posts)


@app.get("/api/reports", response_model=List[PostReport])
def read_reports() -> List[PostReport]:
    """Return the parsed report entries as JSON."""

    return get_reports()


def _validate_search_request(payload: SearchRequest) -> SearchRequest:
    subreddits = [sub.strip() for sub in payload.subreddits if sub.strip()]
    if not subreddits:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one subreddit is required.",
        )
    if payload.time_filter not in ALLOWED_TIME_FILTERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid time filter.",
        )
    if not payload.query.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Query must not be empty.",
        )
    if payload.limit <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Limit must be a positive integer.",
        )
    if payload.comments_limit < -1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Comments limit must be -1 or greater.",
        )
    payload.subreddits = subreddits
    payload.query = payload.query.strip()
    return payload


@app.post("/api/searches", response_model=SearchJob, status_code=status.HTTP_201_CREATED)
def create_search(request: SearchRequest) -> SearchJob:
    """Queue a Reddit search via RQ and return the job metadata."""

    payload = _validate_search_request(request)
    job = create_search_job(
        query=payload.query,
        subreddits=payload.subreddits,
        time_filter=payload.time_filter,
        limit=payload.limit,
        comments_limit=payload.comments_limit,
    )
    try:
        rq_job = queue.enqueue(
            search_runner.run,
            job_timeout=900,
            search_job_id=job.id,
            subreddits=payload.subreddits,
            query=payload.query,
            time_filter=payload.time_filter,
            limit=payload.limit,
            comments_limit=payload.comments_limit,
        )
    except Exception as exc:  # pragma: no cover - depends on Redis availability
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Unable to enqueue search job: {exc}",
        ) from exc
    return set_rq_job_id(job.id, rq_job.id)


@app.get("/", response_class=HTMLResponse)
def render_dashboard(request: Request) -> HTMLResponse:
    """Render the dashboard shell; client fetches data via HTMX."""

    return TEMPLATES.TemplateResponse(
        "report_dashboard.html",
        {"request": request},
    )


__all__ = ["app", "get_reports", "PostReport", "SearchJob", "SearchRequest"]
