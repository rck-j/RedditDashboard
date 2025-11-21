"""Dashboard FastAPI application assembly."""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from src.db.migrations import run_migrations
from src.db.session import engine
from src.infra.cleanup import purge_expired_jobs_by_plan
from src.services.plans import archival_plans, plan_retention_days
from src.ui.dashboard import settings
from src.ui.dashboard.api import api_router
from src.ui.dashboard.auth import auth_router
from src.ui.dashboard.metrics import metrics_router
from src.ui.dashboard.pages import pages_router

logger = logging.getLogger(__name__)


def _build_app() -> FastAPI:
    app = FastAPI(title="Reddit Automation Report Dashboard")
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.AUTH_SECRET_KEY,
        session_cookie="reddash_state",
        max_age=settings.AUTH_TOKEN_TTL_SECONDS,
        same_site="lax",
        https_only=settings.AUTH_COOKIE_SECURE,
    )

    @app.on_event("startup")
    def _init_db() -> None:
        run_migrations(engine)

    async def _cleanup_loop() -> None:
        while True:
            try:
                purge_expired_jobs_by_plan(
                    plan_retention_days(), archive_only_plans=archival_plans()
                )
            except Exception:  # pragma: no cover - defensive logging
                logger.exception("job_cleanup_failed")
            await asyncio.sleep(max(settings.JOB_CLEANUP_INTERVAL_SECONDS, 60))

    @app.on_event("startup")
    async def _schedule_cleanup() -> None:
        if not settings.ENABLE_JOB_CLEANUP or settings.JOB_CLEANUP_INTERVAL_SECONDS <= 0:
            return
        asyncio.create_task(_cleanup_loop())

    app.include_router(auth_router)
    app.include_router(api_router)
    app.include_router(pages_router)
    app.include_router(metrics_router)
    return app


app = _build_app()

__all__ = ["app", "auth_router", "api_router", "pages_router", "metrics_router"]
