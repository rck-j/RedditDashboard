"""SQLModel ORM definitions for persisted Reddit search data."""

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from sqlalchemy import Boolean, Column, DateTime, JSON, String, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, Enum):
    """Lifecycle states for a Reddit search job."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AuthProvider(str, Enum):
    """Supported identity providers for authenticated dashboard users."""

    SYSTEM = "system"
    GOOGLE = "google"
    GITHUB = "github"
    MICROSOFT = "microsoft"
    OKTA = "okta"
    AZURE_AD = "azure_ad"
    CUSTOM = "custom"


class SubscriptionPlan(str, Enum):
    """Simple subscription tier metadata used for authorization."""

    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class User(SQLModel, table=True):
    """Authenticated dashboard user derived from OAuth identity claims."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint(
            "auth_provider",
            "provider_account_id",
            name="uq_users_provider_account",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str | None = Field(
        default=None,
        sa_column=Column(String(255), unique=True, nullable=True),
    )
    display_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
    )
    avatar_url: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
    )
    auth_provider: AuthProvider = Field(
        default=AuthProvider.SYSTEM,
        sa_column=Column(String(32), nullable=False),
    )
    provider_account_id: str = Field(
        sa_column=Column(String(255), nullable=False),
        description="Opaque identifier provided by the upstream IdP.",
    )
    subscription_plan: SubscriptionPlan = Field(
        default=SubscriptionPlan.FREE,
        sa_column=Column(String(32), nullable=False),
    )
    is_active: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, default=True),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    last_login_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    search_jobs: List["SearchJob"] = Relationship(back_populates="user")
    reports: List["PersistedPostReport"] = Relationship(back_populates="user")


class SearchJob(SQLModel, table=True):
    """Metadata for a Reddit search run queued through the dashboard."""

    __tablename__ = "search_jobs"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", nullable=False)
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
    error_detail: Optional[dict] = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description="Structured JobError payload captured when the run fails.",
    )
    average_score: Optional[float] = None
    is_deleted: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, default=False),
    )
    reports: List["PersistedPostReport"] = Relationship(
        back_populates="search_job",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    user: "User" = Relationship(back_populates="search_jobs")
    stats: dict | None = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description="Cached analytics snapshot for the completed job.",
    )


class PersistedPostReport(SQLModel, table=True):
    """Database-backed representation of ``src.services.PostReport``."""

    __tablename__ = "post_reports"

    id: Optional[int] = Field(default=None, primary_key=True)
    search_job_id: int = Field(foreign_key="search_jobs.id", nullable=False)
    user_id: Optional[int] = Field(
        default=None,
        foreign_key="users.id",
        description="Denormalized owner reference for analytics queries.",
    )
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
    user: Optional["User"] = Relationship(back_populates="reports")


__all__ = [
    "AuthProvider",
    "JobStatus",
    "PersistedPostReport",
    "SearchJob",
    "SubscriptionPlan",
    "User",
]
