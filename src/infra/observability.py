"""Logging and metrics helpers shared across the CLI, API, and workers."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict

from prometheus_client import Gauge, Histogram


JOB_DURATION = Histogram(
    "reddash_job_duration_seconds",
    "Wall-clock duration of completed Reddit search jobs.",
)
POSTS_PER_MINUTE = Gauge(
    "reddash_posts_per_minute",
    "Average posts processed per minute for the most recent job.",
)
OPENAI_TOKENS_PER_JOB = Histogram(
    "reddash_openai_tokens_per_job",
    "Total OpenAI tokens consumed per completed job.",
)
QUEUE_DEPTH = Gauge(
    "reddash_queue_depth",
    "Number of Reddit search jobs currently waiting in the queue.",
)


def _serialize(value: Any) -> Any:
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return value


def log_structured(
    logger: logging.Logger,
    level: int,
    event: str,
    /,
    **fields: Any,
) -> None:
    """Emit a JSON-formatted log line with a consistent schema."""

    payload: Dict[str, Any] = {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload.update({k: v for k, v in fields.items() if v is not None})
    logger.log(level, json.dumps(payload, default=_serialize))


class TelemetryRecorder:
    """Stateful helper for recording job metrics and external API activity."""

    def __init__(
        self,
        *,
        logger: logging.Logger,
        job_id: int | str | None = None,
    ) -> None:
        self._logger = logger
        self._job_id = job_id
        self._job_started_at: float | None = None
        self._posts_processed = 0
        self._tokens_consumed = 0

    def job_started(self, *, queued_at: datetime | None = None) -> None:
        self._job_started_at = time.perf_counter()
        log_structured(
            self._logger,
            logging.INFO,
            "job_started",
            job_id=self._job_id,
            queued_at=queued_at,
        )

    def job_progress(self, *, processed: int, total_seen: int | None = None) -> None:
        self._posts_processed = processed
        log_structured(
            self._logger,
            logging.INFO,
            "job_progress",
            job_id=self._job_id,
            processed=processed,
            total_seen=total_seen,
        )

    def job_completed(self, *, status: str = "succeeded") -> None:
        duration = self._duration_seconds()
        if duration is not None:
            JOB_DURATION.observe(duration)
            rate = (self._posts_processed / duration * 60) if duration > 0 else 0
            POSTS_PER_MINUTE.set(rate)
        OPENAI_TOKENS_PER_JOB.observe(max(self._tokens_consumed, 0))
        log_structured(
            self._logger,
            logging.INFO,
            "job_completed",
            job_id=self._job_id,
            status=status,
            duration_seconds=duration,
            posts_processed=self._posts_processed,
            tokens_consumed=self._tokens_consumed,
        )

    def job_failed(self, *, error_code: str, message: str) -> None:
        self.job_completed(status="failed")
        duration = self._duration_seconds()
        log_structured(
            self._logger,
            logging.ERROR,
            "job_failed",
            job_id=self._job_id,
            duration_seconds=duration,
            error_code=error_code,
            message=message,
        )

    def record_post_processed(self, processed: int) -> None:
        self._posts_processed = processed

    def log_reddit_call(
        self,
        *,
        action: str,
        phase: str,
        metadata: Dict[str, Any],
        duration_ms: float | None = None,
        quota: Dict[str, Any] | None = None,
    ) -> None:
        log_structured(
            self._logger,
            logging.INFO,
            "reddit_call",
            job_id=self._job_id,
            action=action,
            phase=phase,
            duration_ms=duration_ms,
            quota=quota,
            **metadata,
        )

    def log_openai_request(self, *, prompt_label: str | None = None) -> None:
        log_structured(
            self._logger,
            logging.INFO,
            "openai_request",
            job_id=self._job_id,
            prompt_label=prompt_label,
            masked_payload=True,
        )

    def log_openai_response(
        self,
        *,
        prompt_label: str | None = None,
        duration_ms: float,
        usage: Dict[str, Any] | None = None,
    ) -> None:
        tokens = self._extract_total_tokens(usage)
        if tokens is not None:
            self._tokens_consumed += tokens
        log_structured(
            self._logger,
            logging.INFO,
            "openai_response",
            job_id=self._job_id,
            prompt_label=prompt_label,
            duration_ms=duration_ms,
            usage=usage,
            masked_payload=True,
        )

    @staticmethod
    def _extract_total_tokens(usage: Dict[str, Any] | None) -> int | None:
        if usage is None:
            return None
        for key in ("total_tokens", "total", "usage_total"):
            raw = usage.get(key)
            if isinstance(raw, int):
                return raw
        tokens = 0
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "input_tokens",
            "output_tokens",
        ):
            raw = usage.get(key)
            if isinstance(raw, int):
                tokens += raw
        return tokens or None

    def _duration_seconds(self) -> float | None:
        if self._job_started_at is None:
            return None
        return time.perf_counter() - self._job_started_at


def set_queue_depth(depth: int) -> None:
    """Update the queue depth gauge."""

    QUEUE_DEPTH.set(depth)


__all__ = [
    "JOB_DURATION",
    "OPENAI_TOKENS_PER_JOB",
    "POSTS_PER_MINUTE",
    "QUEUE_DEPTH",
    "TelemetryRecorder",
    "log_structured",
    "set_queue_depth",
]
