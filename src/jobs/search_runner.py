"""RQ job for running a Reddit automation search."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv

from red import PROMPTS, _save_report, build_openai_client, build_reddit_client
from src.infra.search_jobs import mark_failed, mark_running, mark_succeeded
from src.services import AnalysisReport, AnalyzerDependencies, AutomationAnalyzer, search_posts

load_dotenv()

JOBS_DIR = Path(__file__).resolve().parents[2] / "data" / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)


def run(
    *,
    search_job_id: int,
    subreddits: Sequence[str],
    query: str,
    time_filter: str,
    limit: int,
    comments_limit: int,
) -> str:
    """Execute a Reddit search and persist the resulting analysis report."""

    mark_running(search_job_id)
    reddit_client = build_reddit_client()
    openai_client, model_name = build_openai_client()
    deps = AnalyzerDependencies(
        reddit=reddit_client,
        openai=openai_client,
        openai_model=model_name,
        prompts=PROMPTS,
    )
    analyzer = AutomationAnalyzer(deps)
    generated_at = datetime.now(timezone.utc).isoformat()
    results = []
    try:
        for summary in search_posts(
            reddit_client,
            subreddits=subreddits,
            query=query,
            time_filter=time_filter,
            limit=limit,
        ):
            results.append(
                analyzer.analyze_post(summary, comment_limit=comments_limit)
            )
        report = AnalysisReport(
            generated_at=generated_at,
            query=query,
            time_filter=time_filter,
            posts=results,
        )
        output_path = JOBS_DIR / f"search_job_{search_job_id}.json"
        _save_report(output_path, report)
        mark_succeeded(search_job_id, result_path=str(output_path))
        return str(output_path)
    except Exception as exc:  # pragma: no cover - depends on live APIs
        mark_failed(search_job_id, error=str(exc))
        raise


__all__ = ["run"]
