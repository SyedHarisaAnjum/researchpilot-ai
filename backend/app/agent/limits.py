"""Research budgets for each depth level. Every job stores its own copy in its state."""

from typing import Literal, TypedDict

from app.config import get_settings

Depth = Literal["quick", "standard", "deep"]
DEPTHS: tuple[Depth, ...] = ("quick", "standard", "deep")


class ResearchLimits(TypedDict):
    max_rounds: int
    max_searches_per_round: int
    max_total_searches: int
    max_pages_per_round: int
    max_pages_per_research: int
    max_llm_calls: int
    max_research_seconds: int
    max_total_tokens: int  # prompt + output + thinking tokens across all model calls


def limits_for_depth(depth: Depth) -> ResearchLimits:
    if depth == "quick":
        return ResearchLimits(
            max_rounds=1, max_searches_per_round=3, max_total_searches=3, max_pages_per_round=4,
            max_pages_per_research=4, max_llm_calls=12, max_research_seconds=120, max_total_tokens=150_000,
        )
    if depth == "deep":
        return ResearchLimits(
            max_rounds=4, max_searches_per_round=4, max_total_searches=16, max_pages_per_round=6,
            max_pages_per_research=24, max_llm_calls=70, max_research_seconds=900, max_total_tokens=1_500_000,
        )
    # "standard" comes from .env, so it can be tuned without code changes.
    settings = get_settings()
    return ResearchLimits(
        max_rounds=settings.max_research_rounds,
        max_searches_per_round=settings.max_searches_per_round,
        max_total_searches=settings.max_total_searches,
        max_pages_per_round=settings.max_pages_per_round,
        max_pages_per_research=settings.max_pages_per_research,
        max_llm_calls=settings.max_llm_calls,
        max_research_seconds=settings.max_research_seconds,
        max_total_tokens=settings.max_total_tokens,
    )
