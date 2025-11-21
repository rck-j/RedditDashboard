"""HTML page routes for the dashboard shell."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.ui.dashboard.settings import TEMPLATES

pages_router = APIRouter()


def _page_response(request: Request, template_name: str) -> HTMLResponse:
    """Render a Jinja template with navigation context."""

    return TEMPLATES.TemplateResponse(
        template_name,
        {"request": request, "current_path": request.url.path},
    )


@pages_router.get("/", response_class=HTMLResponse)
def render_landing(request: Request) -> HTMLResponse:
    """Landing page that introduces the multipage dashboard."""

    return _page_response(request, "landing.html")


@pages_router.get("/auth", response_class=HTMLResponse)
def render_auth(request: Request) -> HTMLResponse:
    """Login and signup experience for RedDash."""

    return _page_response(request, "auth.html")


@pages_router.get("/plans", response_class=HTMLResponse)
def render_plan_selection(request: Request) -> HTMLResponse:
    """Let visitors choose a subscription plan before running searches."""

    return _page_response(request, "plan_selection.html")


@pages_router.get("/dashboard", response_class=HTMLResponse)
def render_dashboard(request: Request) -> HTMLResponse:
    """Primary dashboard surface for launching and reviewing jobs."""

    return _page_response(request, "dashboard.html")


@pages_router.get("/preferences", response_class=HTMLResponse)
def render_preferences(request: Request) -> HTMLResponse:
    """User preferences page for notifications and defaults."""

    return _page_response(request, "preferences.html")


__all__ = ["pages_router"]
