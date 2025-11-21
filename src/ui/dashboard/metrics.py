"""Prometheus metrics endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

metrics_router = APIRouter()


@metrics_router.get("/metrics")
def metrics() -> Response:
    """Expose Prometheus metrics for scraping."""

    payload = generate_latest()
    return Response(payload, media_type=CONTENT_TYPE_LATEST)


__all__ = ["metrics_router"]
