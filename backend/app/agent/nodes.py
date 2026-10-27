"""LangGraph nodes. Each takes the current state and returns only the fields it changes.

Failures are recorded in `errors` and the research carries on with what it has.
Budgets (model calls, searches, pages, time) are checked before work is started.
"""

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from app.agent import prompts
from app.agent.planner import create_research_plan
from app.agent.quality import (
    assess_evidence,
    coverage_summary,
    match_subquestion,
    normalize_text,
    quote_in_content,
)
from app.agent.state import ResearchState, error_entry, evidence_id, source_id
from app.config import get_settings
from app.schemas import EvidenceExtraction, EvidenceRecord, GapAnalysis, ResearchPlan, SearchQueries, SourceRecord
from app.services.llm_service import LLMError, LLMRateLimitError, LLMResponseError, LLMTimeoutError, get_llm_service
from app.services.report_service import build_report
from app.services.search_service import (
    SearchAuthError,
    SearchError,
    SearchQuotaError,
    SearchRateLimitError,
    get_search_service,
    normalize_url,
)
from app.services.web_reader import get_web_reader
from app.utils.run_context import tokens_used

logger = logging.getLogger(__name__)

MAX_OPEN_QUESTIONS = 8
# Model calls kept back for the report: one draft and at least one support check.
REPORT_RESERVED_CALLS = 2
TOKEN_RESEARCH_SHARE = 0.85


def error_code(exc: BaseException) -> str:
    """A stable code for each kind of failure, so callers can tell them apart."""
    for kind, code in (
        (LLMTimeoutError, "llm_timeout"), (LLMRateLimitError, "llm_rate_limited"),
        (LLMResponseError, "llm_invalid_output"), (LLMError, "llm_error"),
        (SearchQuotaError, "search_quota_exceeded"), (SearchAuthError, "search_auth_failed"),
        (SearchRateLimitError, "search_rate_limited"), (SearchError, "search_error"),
    ):
        if isinstance(exc, kind):
            return code
    return "internal_error"


# --- Budget ---


def elapsed_seconds(state: ResearchState) -> float:
    return time.time() - state["started_at"]


def budget_exceeded(state: ResearchState, llm_calls_needed: int = 0) -> str | None:
    """Reason the budget can't cover more work, or None. Calls for the report are kept back."""
    limits = state["limits"]
    if elapsed_seconds(state) >= limits["max_research_seconds"]:
        return f"Time limit reached ({limits['max_research_seconds']}s)"
    if state["llm_calls"] + llm_calls_needed > limits["max_llm_calls"] - REPORT_RESERVED_CALLS:
        return f"Model call limit reached ({limits['max_llm_calls']})"
    # Research stops at 85% of the token budget so the report and its checks still fit.
    tokens, max_tokens = tokens_used(), limits.get("max_total_tokens")
    if tokens is not None and max_tokens and tokens >= TOKEN_RESEARCH_SHARE * max_tokens:
        return f"Token limit reached ({tokens:,} of {max_tokens:,})"
    return None


def research_stop_reason(state: ResearchState) -> str | None:
    """Why research should stop now, or None to run another round. Decided by code, not the LLM."""
    limits = state["limits"]
    if state.get("stop_reason"):
        return state["stop_reason"]
    if state.get("is_sufficient"):
        return "Evidence judged sufficient"
    if not state.get("missing_questions"):
        return "No open questions remain"
    if state["round_number"] >= limits["max_rounds"]:
        return f"Round limit reached ({limits['max_rounds']})"
    if len(state["sources"]) >= limits["max_pages_per_research"]:
        return f"Page limit reached ({limits['max_pages_per_research']})"
    if state["search_calls"] >= limits["max_total_searches"]:
        return f"Search limit reached ({limits['max_total_searches']})"
    # Another round needs at least query generation, one extraction and one gap review.
    return budget_exceeded(state, llm_calls_needed=3)


def _unique_queries(queries: list[str], used: list[str]) -> list[str]:
    seen = {normalize_text(q) for q in used}
    unique = []
    for query in queries:
        key = normalize_text(query)
        if key and key not in seen:
            seen.add(key)
            unique.append(" ".join(query.split()))
    return unique


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --- Nodes ---


def fallback_plan(question: str) -> ResearchPlan:
    """Research the question as asked. Used when planning fails for a temporary reason."""
    return ResearchPlan(
        main_question=question, subquestions=[question], search_queries=[question],
        research_goal=f"Answer the question with sourced evidence: {question}",
    )


async def plan_research(state: ResearchState) -> dict[str, Any]:
    limits = state["limits"]
    errors: list[dict[str, Any]] = []
    try:
        plan = await create_research_plan(state["question"])
    except (LLMTimeoutError, LLMRateLimitError, LLMResponseError) as exc:
        # Temporary or output problems: carry on with a simpler plan rather than lose the job.
        plan = fallback_plan(state["question"])
        errors.append(
            error_entry("planning", f"Planning failed ({exc}); researching the question directly", code=error_code(exc))
        )
    except LLMError as exc:
        # Configuration problems (wrong model, bad key): every later step would fail too.
        return {
            "status": "failed",
            "llm_calls": state["llm_calls"] + 1,
            "stop_reason": "Could not create a research plan",
            "errors": [error_entry("planning", f"Could not create a research plan: {exc}", code=error_code(exc))],
        }
    return {
        "status": "searching",
        "plan": plan.model_dump(),
        "round_number": 1,
        "pending_queries": _unique_queries(plan.search_queries, [])[: limits["max_searches_per_round"]],
        "llm_calls": state["llm_calls"] + 1,
        "errors": errors,
    }


async def search_web(state: ResearchState) -> dict[str, Any]:
    limits = state["limits"]
    round_number = state["round_number"]
    if reason := budget_exceeded(state):
        return {
            "status": "reading",
            "pending_queries": [],
            "stop_reason": reason,
            "errors": [error_entry("searching", reason, round_number)],
        }
    remaining = limits["max_total_searches"] - state["search_calls"]
    queries = state["pending_queries"][: max(remaining, 0)]
    if not queries:
        return {"status": "reading", "pending_queries": []}

    service = get_search_service()
    outcomes = await asyncio.gather(*(service.search(q) for q in queries), return_exceptions=True)

    seen = {normalize_url(r["url"]) for r in state["search_results"]}
    new_results, errors, stop_reason = [], [], None
    for query, outcome in zip(queries, outcomes):
        if isinstance(outcome, BaseException):
            errors.append(error_entry("searching", f"Search failed for {query!r}: {outcome}", round_number,
                                      query=query, code=error_code(outcome)))
            if isinstance(outcome, (SearchAuthError, SearchQuotaError)):
                stop_reason = f"Search unavailable: {outcome}"
            continue
        for result in outcome:
            key = normalize_url(result.url)
            if key not in seen:
                seen.add(key)
                new_results.append({**result.model_dump(), "query": query, "round": round_number})

    update: dict[str, Any] = {
        "status": "reading",
        "search_results": new_results,
        "used_queries": state["used_queries"] + queries,
        "pending_queries": [],
        "search_calls": state["search_calls"] + len(queries),
        "errors": errors,
    }
    if stop_reason:
        update["stop_reason"] = stop_reason
    return update


def _pick_pages(state: ResearchState, limit: int) -> list[dict[str, Any]]:
    """Unread results, this round's first, taking one per query in turn so every query gets read."""
    attempted = {normalize_url(s["url"]) for s in state["sources"]}
    unread = [r for r in state["search_results"] if normalize_url(r["url"]) not in attempted]
    by_query: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in sorted(unread, key=lambda r: r["round"] != state["round_number"]):
        by_query[result["query"]].append(result)

    picked = []
    while len(picked) < limit and any(by_query.values()):
        for results in by_query.values():
            if results and len(picked) < limit:
                picked.append(results.pop(0))
    return picked


async def read_pages(state: ResearchState) -> dict[str, Any]:
    limits = state["limits"]
    round_number = state["round_number"]
    limit = min(limits["max_pages_per_round"], limits["max_pages_per_research"] - len(state["sources"]))
    if reason := budget_exceeded(state):
        return {"status": "extracting", "stop_reason": reason, "errors": [error_entry("reading", reason, round_number)]}

    chosen = _pick_pages(state, limit) if limit > 0 else []
    if not chosen:
        return {"status": "extracting", "errors": [error_entry("reading", "No new pages to read", round_number)]}

    results = await get_web_reader().read_many([c["url"] for c in chosen])
    sources, pages, errors = [], [], []
    for i, (candidate, page) in enumerate(zip(chosen, results)):
        sid = source_id(len(state["sources"]) + i + 1)
        sources.append(
            SourceRecord(
                source_id=sid,
                url=page.url,
                final_url=page.final_url,
                domain=(urlsplit(page.final_url or page.url).hostname or "").removeprefix("www."),
                title=page.title or candidate["title"],
                description=page.description,
                published_at=page.published_at,
                retrieved_at=page.retrieved_at or _now(),
                fetch_status=page.status,
                error=page.error,
                query=candidate["query"],
                round=round_number,
            ).model_dump()
        )
        if page.status == "success":
            pages.append({"source_id": sid, "round": round_number, "status": page.status, "content": page.content})
        else:
            errors.append(error_entry("reading", f"{page.status}: {page.error}", round_number, url=page.url))

    if not pages:
        errors.append(
            error_entry("reading", "No page could be read this round; search snippets are not used as evidence", round_number)
        )
    return {"status": "extracting", "sources": sources, "pages": pages, "errors": errors}


def _verify_evidence(
    result: EvidenceExtraction,
    page: dict[str, Any],
    source: dict[str, Any],
    subquestions: list[str],
    errors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only evidence whose excerpt really is in the page, tied to the source it came from."""
    kept, rejected = [], 0
    for item in result.evidence:
        if not quote_in_content(item.quote, page["content"]):
            rejected += 1
            continue
        kept.append(
            {
                "claim": item.claim,
                # The ID comes from our record, whatever the model wrote.
                "source_id": source["source_id"],
                "source_url": source["final_url"] or source["url"],
                "source_title": source["title"],
                "supporting_excerpt": item.quote.strip(),
                "retrieved_at": source["retrieved_at"],
                "subquestion": match_subquestion(item.subquestion, subquestions) or item.subquestion,
                "confidence": item.confidence,
                "round": page["round"],
            }
        )
    if rejected:
        errors.append(
            error_entry(
                "extracting",
                f"Discarded {rejected} evidence item(s) from {source['source_id']}: excerpt not found in the page",
                page["round"],
                url=source["url"],
                discarded=rejected,
            )
        )
    return kept


async def extract_evidence(state: ResearchState) -> dict[str, Any]:
    round_number = state["round_number"]
    pages = [p for p in state["pages"] if p["round"] == round_number]
    sources = {s["source_id"]: s for s in state["sources"]}
    subquestions = state["plan"]["subquestions"]
    # Later rounds also look for evidence on the open questions.
    targets = subquestions + [q for q in state["missing_questions"] if q not in subquestions]

    llm = get_llm_service()
    semaphore = asyncio.Semaphore(get_settings().llm_concurrency)
    errors: list[dict[str, Any]] = []
    calls = 0
    stop_reason = None

    async def extract(page: dict[str, Any]) -> list[dict[str, Any]]:
        nonlocal calls, stop_reason
        source = sources[page["source_id"]]
        async with semaphore:
            if reason := budget_exceeded({**state, "llm_calls": state["llm_calls"] + calls}, llm_calls_needed=1):
                stop_reason = reason
                errors.append(error_entry("extracting", f"Skipped {source['source_id']}: {reason}", round_number))
                return []
            calls += 1
            try:
                result = await llm.generate_structured(
                    prompts.build_evidence_prompt(
                        state["question"], targets, source["source_id"], source["title"], source["url"], page["content"]
                    ),
                    EvidenceExtraction,
                    system_instruction=prompts.EVIDENCE_INSTRUCTION,
                )
            except LLMError as exc:
                errors.append(error_entry("extracting", f"Extraction failed for {source['source_id']}: {exc}", round_number))
                return []
        return _verify_evidence(result, page, source, targets, errors)

    batches = await asyncio.gather(*(extract(p) for p in pages))
    # Numbered after gathering so IDs follow page order, not finishing order.
    items = [item for batch in batches for item in batch]
    evidence = [
        EvidenceRecord(evidence_id=evidence_id(len(state["evidence"]) + i + 1), **item).model_dump()
        for i, item in enumerate(items)
    ]
    update: dict[str, Any] = {
        "status": "analyzing",
        "evidence": evidence,
        "llm_calls": state["llm_calls"] + calls,
        "errors": errors,
    }
    if stop_reason:
        update["stop_reason"] = stop_reason
    return update


def _cited_sources(state: ResearchState) -> list[dict[str, Any]]:
    """Source records that produced evidence, in ID order."""
    ids = {e["source_id"] for e in state["evidence"]}
    return [s for s in state["sources"] if s["source_id"] in ids]


async def evaluate_gaps(state: ResearchState) -> dict[str, Any]:
    round_number = state["round_number"]
    subquestions = state["plan"]["subquestions"]
    quality = assess_evidence(subquestions, state["evidence"], state["sources"])
    update: dict[str, Any] = {"status": "analyzing", "quality": quality, "gap_analysis": {}}
    errors: list[dict[str, Any]] = []
    gap: GapAnalysis | None = None
    calls = 0

    if not state["evidence"]:
        errors.append(error_entry("analyzing", "No evidence collected yet", round_number))
    elif reason := budget_exceeded(state, llm_calls_needed=1):
        update["stop_reason"] = reason
        errors.append(error_entry("analyzing", f"Skipped gap review: {reason}", round_number))
    else:
        calls = 1
        try:
            gap = await get_llm_service().generate_structured(
                prompts.build_gap_prompt(
                    state["question"], subquestions, state["evidence"], _cited_sources(state),
                    coverage_summary(quality), round_number, state["limits"]["max_rounds"],
                ),
                GapAnalysis,
                system_instruction=prompts.GAP_INSTRUCTION,
            )
        except LLMError as exc:
            errors.append(error_entry("analyzing", f"Gap review failed: {exc}", round_number))

    # Code has the final say: a subquestion with no evidence is open, whatever the LLM says.
    if gap:
        open_questions = quality["missing"] + gap.missing_questions
        update["gap_analysis"] = gap.model_dump()
    else:
        open_questions = quality["missing"] + quality["weak"]
    update["missing_questions"] = _unique_queries(open_questions, [])[:MAX_OPEN_QUESTIONS]
    update["is_sufficient"] = bool(gap and gap.is_sufficient) and not quality["missing"]
    update["llm_calls"] = state["llm_calls"] + calls
    update["errors"] = errors
    return update


async def plan_next_round(state: ResearchState) -> dict[str, Any]:
    """Write new queries aimed at the open questions, never repeating earlier ones."""
    limits = state["limits"]
    round_number = state["round_number"] + 1
    limit = min(limits["max_searches_per_round"], limits["max_total_searches"] - state["search_calls"])
    open_questions = state["missing_questions"]
    errors: list[dict[str, Any]] = []
    queries: list[str] = []
    calls = 0

    if not budget_exceeded(state, llm_calls_needed=1):
        calls = 1
        instruction, prompt = prompts.build_query_prompt(state["question"], open_questions, state["used_queries"], limit)
        try:
            result = await get_llm_service().generate_structured(prompt, SearchQueries, system_instruction=instruction)
            queries = result.queries
        except LLMError as exc:
            errors.append(error_entry("planning", f"Query generation failed, searching the open questions directly: {exc}", round_number))

    if not queries:
        queries = open_questions
    queries = _unique_queries(queries, state["used_queries"])[: max(limit, 0)]

    update: dict[str, Any] = {
        "status": "searching",
        "round_number": round_number,
        "pending_queries": queries,
        "llm_calls": state["llm_calls"] + calls,
        "errors": errors,
    }
    if not queries:
        update["stop_reason"] = "No new search queries left to try"
    return update


async def finalize_report(state: ResearchState) -> dict[str, Any]:
    result = await build_report(state, research_stop_reason(state))
    return {
        "status": "completed",
        "report": result.report,
        "llm_calls": state["llm_calls"] + result.llm_calls,
        "errors": result.errors,
    }
