"""Reddit automation opportunity analyzer."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

import praw
from dotenv import load_dotenv
from openai import OpenAI

from src import config as app_config
from src.services import (
    AnalysisReport,
    AnalyzerDependencies,
    AutomationAnalyzer,
    PostReport,
    search_posts,
)


load_dotenv()


DEFAULT_OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")


def _require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(f"Missing {key}; define it in .env or your shell.")
    return value


PROMPTS = app_config.get_prompts()


def build_reddit_client() -> praw.Reddit:
    return praw.Reddit(
        client_id=_require_env("PRAW_CLIENT_ID"),
        client_secret=_require_env("PRAW_CLIENT_SECRET"),
        user_agent=_require_env("PRAW_USER_AGENT"),
    )


def build_openai_client() -> Tuple[OpenAI, str]:
    client = OpenAI(api_key=_require_env("OPENAI_API_KEY"))
    if not hasattr(client.responses, "parse"):
        raise RuntimeError(
            "openai client does not support responses.parse; upgrade the package."
        )
    return client, DEFAULT_OPENAI_MODEL


def _print_report_entry(report: PostReport) -> None:
    insight = report.automation_insight
    initial = report.initial_assessment
    tools = ", ".join(insight.required_tools) or "n/a"
    print(
        f"[{report.subreddit}] {report.created} | {report.score} | "
        f"{report.num_comments} | {report.title} | {report.url}"
    )
    print(
        f"    Initial Assessment: {'YES' if initial.is_automation else 'NO'} - "
        f"{initial.rationale}"
    )
    print(
        f"    Deep Dive: {insight.deep_analysis} "
        f"(Summary: {insight.automation_summary}; "
        f"Complexity: {insight.automation_complexity}; Tools: {tools})"
    )


def _save_report(path: Path, report: AnalysisReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = report.model_dump(mode="json")
    payload["total_posts"] = report.total_posts
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--subs",
        nargs="+",
        default=["smallbusiness", "Entrepreneur"],
        help="Subreddits to search.",
    )
    parser.add_argument(
        "--query",
        default='(agent OR "ai agent" OR agentic OR automation) '
        "(small business OR smb OR entrepreneur)",
        help="Search query to submit to Reddit.",
    )
    parser.add_argument(
        "--time-filter",
        default=app_config.SEARCH_PARAMETERS.default_time_filter,
        choices=list(app_config.SEARCH_PARAMETERS.time_filters),
        help="Reddit time filter for the search.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=app_config.SEARCH_PARAMETERS.limit_default,
        help="Maximum posts to retrieve per subreddit.",
    )
    parser.add_argument(
        "--comments-limit",
        type=int,
        default=app_config.SEARCH_PARAMETERS.comments_limit_default,
        help="Maximum comments to retrieve during deep analysis (-1 for all).",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=None,
        help="Optional destination for saving the JSON report.",
    )
    args = parser.parse_args()
    _validate_cli_args(parser, args)
    return args


def _validate_cli_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.limit <= 0 or args.limit > app_config.SEARCH_PARAMETERS.limit_max:
        parser.error(
            f"--limit must be between 1 and {app_config.SEARCH_PARAMETERS.limit_max}."
        )
    if args.comments_limit < -1:
        parser.error("--comments-limit must be -1 or greater.")
    if (
        args.comments_limit != -1
        and args.comments_limit > app_config.SEARCH_PARAMETERS.comments_limit_max
    ):
        parser.error(
            "--comments-limit must be -1 or less than or equal to "
            f"{app_config.SEARCH_PARAMETERS.comments_limit_max}."
        )


def main() -> None:
    args = parse_args()
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
    results: List[PostReport] = []
    print("Streaming Reddit results and capturing in-memory report...")
    for post_summary in search_posts(
        reddit_client,
        subreddits=args.subs,
        query=args.query,
        time_filter=args.time_filter,
        limit=args.limit,
    ):
        report = analyzer.analyze_post(
            post_summary, comment_limit=args.comments_limit
        )
        results.append(report)
        _print_report_entry(report)
    final_report = AnalysisReport(
        generated_at=generated_at,
        query=args.query,
        time_filter=args.time_filter,
        posts=results,
    )
    print(f"Processed {final_report.total_posts} posts total.")
    if args.report_path:
        _save_report(args.report_path, final_report)
        print(f"Final report available at {args.report_path}")


if __name__ == "__main__":
    main()
