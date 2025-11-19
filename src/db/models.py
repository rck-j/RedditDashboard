"""SQLModel ORM definitions for persisted Reddit search data."""

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from sqlalchemy import Boolean, Column, DateTime, JSON
from sqlmodel import Field, Relationship, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, Enum):
    """Lifecycle states for a Reddit search job."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SearchJob(SQLModel, table=True):
    """Metadata for a Reddit search run queued through the dashboard."""

    __tablename__ = "search_jobs"

    id: Optional[int] = Field(default=None, primary_key=True)
    subreddits: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )
    query: str
    time_filter: str
    limit: int
    comments_limit: int
    status: JobStatus = Field(default=JobStatus.QUEUED)
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    started_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )
    finished_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )
    processed_count: int = 0
    total_count: int = 0
    error_message: Optional[str] = None
    average_score: Optional[float] = None
    is_deleted: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, default=False),
    )
    reports: List["PersistedPostReport"] = Relationship(
        back_populates="search_job",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class PersistedPostReport(SQLModel, table=True):
    """Database-backed representation of ``src.services.PostReport``."""

    __tablename__ = "post_reports"

    id: Optional[int] = Field(default=None, primary_key=True)
    search_job_id: int = Field(foreign_key="search_jobs.id", nullable=False)
    submission_id: str = Field(index=True)
    subreddit: str
    title: str
    url: str
    permalink: str
    created: str
    created_utc: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    score: int
    num_comments: int
    automation_complexity: str
    required_tools: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )
    insight_text: str
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    search_job: Optional["SearchJob"] = Relationship(back_populates="reports")


__all__ = ["JobStatus", "PersistedPostReport", "SearchJob"]
