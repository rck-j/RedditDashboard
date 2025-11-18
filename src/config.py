"""Shared runtime configuration for search parameters and analyzer prompts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_PATH = REPO_ROOT / "config" / "prompts.json"


@dataclass(frozen=True)
class SearchParameterConfig:
    """Container for Reddit search parameter defaults and guardrails."""

    time_filters: Tuple[str, ...] = (
        "hour",
        "day",
        "week",
        "month",
        "year",
        "all",
    )
    default_time_filter: str = "month"
    limit_default: int = 50
    limit_max: int = 200
    comments_limit_default: int = 5
    comments_limit_max: int = 25
    comments_allow_unlimited: bool = True


SEARCH_PARAMETERS = SearchParameterConfig()

DEFAULT_PROMPTS: Dict[str, str] = {
    "initial_assessment": (
        "You are an automation strategist supporting small businesses and "
        "entrepreneurs. Review the Reddit post details and decide whether it "
        "indicates a meaningful automation or agent opportunity. Return a "
        "binary decision and a short explanation."
    ),
    "deep_assessment": (
        "You now have the full Reddit submission (post body plus sampled "
        "comments). Assess whether an automation or agent solution could "
        "meaningfully help the author or community. Focus on friction, "
        "repetition, coordination gaps, and the Move, Minimally ethos so "
        "computers do the heavy lifting."
    ),
}


def _load_prompt_overrides(path: Path) -> Dict[str, str]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            overrides = json.load(handle)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        print(f"Warning: could not parse prompts file ({exc}); using defaults.")
        return {}
    return {k: v for k, v in overrides.items() if isinstance(v, str)}


@lru_cache(maxsize=1)
def get_prompts() -> Dict[str, str]:
    """Return analyzer prompts merged with optional overrides."""

    prompts = DEFAULT_PROMPTS.copy()
    prompts.update(_load_prompt_overrides(PROMPTS_PATH))
    return prompts


def get_app_config() -> Dict[str, Any]:
    """Structured configuration exposed via the public API."""

    return {
        "time_filters": {
            "options": list(SEARCH_PARAMETERS.time_filters),
            "default": SEARCH_PARAMETERS.default_time_filter,
        },
        "limits": {
            "posts": {
                "default": SEARCH_PARAMETERS.limit_default,
                "max": SEARCH_PARAMETERS.limit_max,
                "allow_unlimited": False,
            },
            "comments": {
                "default": SEARCH_PARAMETERS.comments_limit_default,
                "max": SEARCH_PARAMETERS.comments_limit_max,
                "allow_unlimited": SEARCH_PARAMETERS.comments_allow_unlimited,
            },
        },
        "prompts": get_prompts(),
    }


__all__ = [
    "SEARCH_PARAMETERS",
    "get_app_config",
    "get_prompts",
]
