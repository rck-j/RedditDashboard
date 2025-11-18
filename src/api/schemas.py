"""Pydantic request/response payloads for the public API."""

from __future__ import annotations

from datetime import datetime
from typing import List

from pydantic import BaseModel, ConfigDict, Field

from src.db.models import JobStatus


class SearchRequest(BaseModel):
    """Request payload for launching a new Reddit search."""

    subreddits: List[str] = Field(
        default_factory=lambda: ["smallbusiness", "Entrepreneur"],
        description="One or more subreddits to query",
    )
    query: str = Field(
        default='(agent OR "ai agent" OR agentic OR automation) '
        "(small business OR smb OR entrepreneur)",
        description="Search query passed to Reddit",
    )
    time_filter: str = Field(default="month")
    limit: int = Field(default=50)
    comments_limit: int = Field(default=5)


class PersistedPostReportSchema(BaseModel):
    """Serialized representation of a persisted automation insight."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    search_job_id: int
    submission_id: str
    subreddit: str
    title: str
    url: str
    permalink: str
    created: str
    score: int
    num_comments: int
    automation_complexity: str
    required_tools: List[str] = Field(default_factory=list)
    insight_text: str
    created_at: datetime


class SearchJobStats(BaseModel):
    """Aggregated counters for a given search job."""

    processed_count: int
    total_count: int
    average_score: float | None = None
    report_count: int


class SearchJobResponse(BaseModel):
    """Envelope for search job metadata (optionally with nested posts)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    subreddits: List[str]
    query: str
    time_filter: str
    limit: int
    comments_limit: int
    status: JobStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None
    stats: SearchJobStats
    reports: List[PersistedPostReportSchema] | None = None


class SearchJobListResponse(BaseModel):
    """Paginated list of search job responses."""

    total: int
    page: int
    page_size: int
    items: List[SearchJobResponse]


__all__ = [
    "PersistedPostReportSchema",
    "SearchJobListResponse",
    "SearchJobResponse",
    "SearchJobStats",
    "SearchRequest",
]
