"""Service-layer utilities used by the CLI and FastAPI apps."""

from .analyzer import (
    AnalysisReport,
    AnalyzerDependencies,
    AutomationAnalyzer,
    AutomationInsight,
    InitialAssessment,
    PostReport,
    fetch_full_post,
    search_posts,
    summarize_post,
)
from .analytics import (
    SummaryMetrics,
    calculate_average_comment_count,
    calculate_average_score,
    calculate_automation_percentage,
    calculate_total_posts,
    summarize_reports,
)

__all__ = [
    "AnalysisReport",
    "AnalyzerDependencies",
    "AutomationAnalyzer",
    "AutomationInsight",
    "InitialAssessment",
    "PostReport",
    "SummaryMetrics",
    "calculate_average_comment_count",
    "calculate_average_score",
    "calculate_automation_percentage",
    "calculate_total_posts",
    "fetch_full_post",
    "summarize_reports",
    "search_posts",
    "summarize_post",
]
