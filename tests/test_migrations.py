from sqlmodel import Session, create_engine, select
from sqlalchemy import inspect

from src.db.migrations import run_migrations
from src.db.models import AuthProvider, PersistedPostReport, SearchJob, User


def test_run_migrations_creates_tables_and_system_user():
    engine = create_engine("sqlite://")

    # Run migrations on a fresh database to ensure they succeed without extra args.
    run_migrations(engine)

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"users", "search_jobs", "post_reports"}.issubset(tables)

    # Validate that the migration created the system user and user_id columns exist.
    with Session(engine) as session:
        system_user = session.exec(
            select(User).where(User.auth_provider == AuthProvider.SYSTEM)
        ).one()
        assert system_user.provider_account_id == "system"

        # Touch the columns via SQLAlchemy reflection to ensure they were added.
        search_job_columns = {col["name"] for col in inspector.get_columns("search_jobs")}
        post_report_columns = {col["name"] for col in inspector.get_columns("post_reports")}
        assert "user_id" in search_job_columns
        assert "user_id" in post_report_columns

        # Ensure inserts with the user_id column succeed post-migration.
        job = SearchJob(
            user_id=system_user.id,
            subreddits=["test"],
            query="query",
            time_filter="all",
            limit=1,
            comments_limit=0,
        )
        session.add(job)
        session.commit()

        report = PersistedPostReport(
            search_job_id=job.id,
            user_id=system_user.id,
            submission_id="abc",
            subreddit="test",
            title="t",
            url="https://example.com",
            permalink="/r/test/abc",
            created="2024-01-01",
            score=1,
            num_comments=0,
            automation_complexity="low",
            required_tools=[],
            insight_text="hi",
        )
        session.add(report)
        session.commit()
