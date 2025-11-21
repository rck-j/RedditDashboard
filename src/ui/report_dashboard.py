"""Web dashboard for viewing Reddit automation reports."""
from __future__ import annotations

from src.api.schemas import SearchRequest
from src.ui.dashboard.api import api_router, get_reports
from src.ui.dashboard.app import app, auth_router, metrics_router, pages_router
from src.ui.dashboard.reports import PostReport

__all__ = [
    "app",
    "auth_router",
    "api_router",
    "pages_router",
    "metrics_router",
    "get_reports",
    "PostReport",
    "SearchRequest",
]
