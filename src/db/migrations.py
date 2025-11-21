"""Lightweight migrations for existing dashboard deployments."""

from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from src.db.repositories import user_repo


@contextmanager
def _session_scope(engine: Engine):
    session = Session(engine)
    try:
        yield session
        session.commit()
    finally:
        session.close()


def _table_columns(inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def _ensure_user_id_column(engine: Engine, table_name: str) -> None:
    inspector = inspect(engine)
    columns = _table_columns(inspector, table_name)
    if "user_id" in columns:
        return
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"ALTER TABLE {table_name} ADD COLUMN user_id INTEGER"
        )


def _ensure_password_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    columns = _table_columns(inspector, "users")
    statements: list[str] = []
    if "password_hash" not in columns:
        statements.append("ALTER TABLE users ADD COLUMN password_hash VARCHAR(255)")
    if "password_salt" not in columns:
        statements.append("ALTER TABLE users ADD COLUMN password_salt VARCHAR(255)")
    if not statements:
        return
    with engine.begin() as connection:
        for stmt in statements:
            connection.exec_driver_sql(stmt)


def _backfill_owner(engine: Engine) -> None:
    with _session_scope(engine) as session:
        system_user = user_repo.ensure_system_user(session)
        session.exec(
            text(
                "UPDATE search_jobs SET user_id = :user_id "
                "WHERE user_id IS NULL"
            ).bindparams(user_id=system_user.id)
        )
        session.exec(
            text(
                "UPDATE post_reports SET user_id = :user_id "
                "WHERE user_id IS NULL"
            ).bindparams(user_id=system_user.id)
        )


def run_migrations(engine: Engine) -> None:
    """Apply schema changes that `SQLModel.metadata.create_all` cannot cover."""

    SQLModel.metadata.create_all(engine)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "users" not in tables:
        # Nothing else to do when the database is new.
        return

    _ensure_password_columns(engine)
    if "search_jobs" in tables:
        _ensure_user_id_column(engine, "search_jobs")
    if "post_reports" in tables:
        _ensure_user_id_column(engine, "post_reports")
    _backfill_owner(engine)


__all__ = ["run_migrations"]
