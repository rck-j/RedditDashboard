"""Pydantic request/response payloads for the public API."""

from __future__ import annotations

from datetime import datetime
import re
from typing import List

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.db.models import JobStatus

DEFAULT_SUBREDDITS = ("smallbusiness", "Entrepreneur")
DEFAULT_QUERY = (
    '(agent OR "ai agent" OR agentic OR automation) '
    "(small business OR smb OR entrepreneur)"
)
ALLOWED_TIME_FILTERS = {"day", "week", "month", "year", "all"}
SUBREDDIT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]{2,20}$")


class SearchRequest(BaseModel):
    """Request payload for launching a new Reddit search."""

    subreddits: List[str] = Field(
        default_factory=lambda: list(DEFAULT_SUBREDDITS),
        description="One or more subreddits to query",
    )
    query: str = Field(
        default=DEFAULT_QUERY,
        description="Search query passed to Reddit",
    )
    time_filter: str = Field(default="month")
    limit: int = Field(default=50)
    comments_limit: int = Field(default=5)

    @field_validator("subreddits", mode="before")
    @classmethod
    def _coerce_subreddits(cls, value):
        if value is None:
            return list(DEFAULT_SUBREDDITS)
        return value

    @field_validator("subreddits")
    @classmethod
    def _validate_subreddits(cls, value: List[str]) -> List[str]:
        normalized: List[str] = []
        for subreddit in value:
            if subreddit is None:
                continue
            cleaned = subreddit.strip()
            if cleaned.lower().startswith("r/"):
                cleaned = cleaned[2:]
            if not cleaned:
                continue
            if not SUBREDDIT_PATTERN.fullmatch(cleaned):
                raise ValueError(
                    "Subreddit names must contain letters, numbers, or underscores "
                    "without spaces (e.g., 'smallbusiness')."
                )
            normalized.append(cleaned)
        if not normalized:
            raise ValueError("At least one subreddit is required.")
        return normalized

    @field_validator("query")
    @classmethod
    def _validate_query(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("Query must not be empty.")
        return cleaned

    @field_validator("time_filter")
    @classmethod
    def _validate_time_filter(cls, value: str) -> str:
        normalized = (value or "").lower()
        if normalized not in ALLOWED_TIME_FILTERS:
            raise ValueError(
                "Time filter must be one of: "
                + ", ".join(sorted(ALLOWED_TIME_FILTERS))
                + "."
            )
        return normalized

    @field_validator("limit")
    @classmethod
    def _validate_limit(cls, value: int) -> int:
        if value is None:
            return 50
        if value <= 0:
            raise ValueError("Limit must be a positive integer.")
        return value

    @field_validator("comments_limit")
    @classmethod
    def _validate_comments_limit(cls, value: int) -> int:
        if value is None:
            return 5
        if value < -1:
            raise ValueError("Comments limit must be -1 or greater.")
        return value


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
