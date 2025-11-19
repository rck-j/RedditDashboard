"""Pydantic request/response payloads for the public API."""

from __future__ import annotations

from datetime import datetime
import re
from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.db.models import JobStatus
from src.jobs.job_errors import JobError
from src.services.analytics import AUTOMATION_COMPLEXITY_LEVELS
from src.config import SEARCH_PARAMETERS

DEFAULT_SUBREDDITS = ("smallbusiness", "Entrepreneur")
DEFAULT_QUERY = (
    '(agent OR "ai agent" OR agentic OR automation) '
    "(small business OR smb OR entrepreneur)"
)
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
    time_filter: str = Field(default=SEARCH_PARAMETERS.default_time_filter)
    limit: int = Field(default=SEARCH_PARAMETERS.limit_default)
    comments_limit: int = Field(default=SEARCH_PARAMETERS.comments_limit_default)

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
        allowed = set(SEARCH_PARAMETERS.time_filters)
        if normalized not in allowed:
            raise ValueError(
                "Time filter must be one of: "
                + ", ".join(sorted(allowed))
                + "."
            )
        return normalized

    @field_validator("limit")
    @classmethod
    def _validate_limit(cls, value: int) -> int:
        if value is None:
            return SEARCH_PARAMETERS.limit_default
        if value <= 0:
            raise ValueError("Limit must be a positive integer.")
        if value > SEARCH_PARAMETERS.limit_max:
            raise ValueError(
                f"Limit must be less than or equal to {SEARCH_PARAMETERS.limit_max}."
            )
        return value

    @field_validator("comments_limit")
    @classmethod
    def _validate_comments_limit(cls, value: int) -> int:
        if value is None:
            return SEARCH_PARAMETERS.comments_limit_default
        if value < -1:
            raise ValueError("Comments limit must be -1 or greater.")
        if value == -1:
            return value
        if value > SEARCH_PARAMETERS.comments_limit_max:
            raise ValueError(
                "Comments limit must be less than or equal to "
                f"{SEARCH_PARAMETERS.comments_limit_max} or -1 for all comments."
            )
        return value


class TimeFilterMetadata(BaseModel):
    options: List[str]
    default: str


class LimitMetadata(BaseModel):
    default: int
    max: int
    allow_unlimited: bool = False


class AppConfigResponse(BaseModel):
    time_filters: TimeFilterMetadata
    limits: dict[str, LimitMetadata]
    prompts: dict[str, str]


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
    created_utc: datetime
    score: int
    num_comments: int
    automation_complexity: str
    required_tools: List[str] = Field(default_factory=list)
    insight_text: str
    created_at: datetime


class TimelineBucketStat(BaseModel):
    """Counts of total/automation posts for a single bucket."""

    bucket_start: datetime = Field(
        description="Start timestamp of the UTC bucket (inclusive)."
    )
    bucket_end: datetime = Field(
        description="End timestamp of the UTC bucket (exclusive)."
    )
    total_posts: int = Field(description="Total persisted posts in the bucket.")
    automation_posts: int = Field(
        description="Posts flagged as automation opportunities in the bucket."
    )


class TimelineStats(BaseModel):
    """Timeline buckets for rendering trend charts."""

    bucket_size: Literal["daily", "weekly"]
    buckets: List[TimelineBucketStat]


class SearchJobSummaryStats(BaseModel):
    """Summary metrics derived from persisted post reports."""

    total_posts: int
    automation_percentage: float | None = None
    average_score: float | None = None
    average_comment_count: float | None = None


class ComplexityTimelineBucket(BaseModel):
    """Automation vs. non-automation counts per UTC date bucket."""

    bucket: str = Field(description="ISO date (YYYY-MM-DD) rounded in UTC")
    automation_count: int = Field(
        description="Reports whose complexity matches automation-ready values."
    )
    non_automation_count: int = Field(
        description="Reports lacking concrete automation detail (unknown / n/a)."
    )


class ComplexityDistributionStats(BaseModel):
    """Normalized automation complexity counts/percentages for a job."""

    total: int = Field(description="Total number of persisted reports.")
    counts: dict[str, int] = Field(
        description=(
            "Number of reports per normalized automation complexity value."
            " Frontend charts expect keys at least for: "
            + ", ".join(AUTOMATION_COMPLEXITY_LEVELS)
            + "."
        )
    )
    percentages: dict[str, float] = Field(
        description="Percentage share (0-100) for each automation complexity key."
    )
    timeline: List[ComplexityTimelineBucket] | None = Field(
        default=None,
        description="Optional automation vs non-automation counts per UTC date bucket.",
    )


class TopSubredditStat(BaseModel):
    subreddit: str
    count: int


class TopKeywordStat(BaseModel):
    keyword: str
    count: int


class ToolStat(BaseModel):
    label: str
    count: int


class SearchJobStats(BaseModel):
    """Aggregated counters and derived analytics for a search job."""

    processed_count: int
    total_count: int
    average_score: float | None = None
    report_count: int
    summary: SearchJobSummaryStats | None = None
    complexity: ComplexityDistributionStats | None = None
    timeline: TimelineStats | None = None
    subreddits: List[TopSubredditStat] | None = None
    keywords: List[TopKeywordStat] | None = None
    tools: List[ToolStat] | None = None


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
    error: JobError | None = None
    has_partial_results: bool = False
    stats: SearchJobStats
    reports: List[PersistedPostReportSchema] | None = None


class SearchJobListResponse(BaseModel):
    """Paginated list of search job responses."""

    total: int
    page: int
    page_size: int
    items: List[SearchJobResponse]


__all__ = [
    "AppConfigResponse",
    "ComplexityDistributionStats",
    "ComplexityTimelineBucket",
    "LimitMetadata",
    "PersistedPostReportSchema",
    "SearchJobListResponse",
    "SearchJobResponse",
    "SearchJobSummaryStats",
    "SearchJobStats",
    "TopKeywordStat",
    "TimelineBucketStat",
    "TimelineStats",
    "ToolStat",
    "TopSubredditStat",
    "SearchRequest",
    "TimeFilterMetadata",
]
