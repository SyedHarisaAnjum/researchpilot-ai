"""Shared state that travels between the LangGraph nodes.

Everything in the state is plain JSON data (str, int, bool, list, dict), so a
state can be saved to the database, compared in tests and inspected in logs.
Pydantic models are stored with `.model_dump()`, never as objects.

Fields marked `Accumulated` use a reducer: a node returns only its NEW items and
LangGraph appends them to the existing list. Returning the whole list would add
duplicates. Every other field is replaced by whatever a node returns.
"""

import json
import operator
import re
import time
import uuid
from typing import Annotated, Any, Literal, TypedDict

from app.agent.limits import Depth, ResearchLimits, limits_for_depth

ResearchStatus = Literal[
    "pending",
    "planning",
    "searching",
    "reading",
    "extracting",
    "analyzing",
    "writing",
    "completed",
    "failed",
]

Accumulated = Annotated[list[dict[str, Any]], operator.add]


class ResearchState(TypedDict, total=False):
    research_id: str
    question: str
    status: ResearchStatus

    # ResearchPlan.model_dump()
    plan: dict[str, Any]

    # Research loop
    round_number: int
    pending_queries: list[str]  # queries to run in the current round
    used_queries: list[str]  # every query run so far, so none is repeated

    # SearchResult.model_dump() plus "round"
    search_results: Accumulated
    # SourceRecord.model_dump(): one per fetched URL, with a stable ID (S01, S02, ...)
    sources: Accumulated
    # Page text, kept apart from source metadata: {"source_id", "round", "status", "content"}
    pages: Accumulated
    # EvidenceRecord.model_dump(): a verified claim pointing to a source by source_id
    evidence: Accumulated

    # Latest round's review: code-computed coverage (quality.py) and the LLM's GapAnalysis
    quality: dict[str, Any]
    gap_analysis: dict[str, Any]
    missing_questions: list[str]
    is_sufficient: bool

    # Budget for this job (limits.py, chosen by depth) and counters checked against it
    depth: Depth
    limits: ResearchLimits
    started_at: float  # Unix timestamp
    llm_calls: int
    search_calls: int
    stop_reason: str | None  # set when a limit or fatal error ends the research early

    # FinalReport.model_dump() (app/services/report_service.py)
    report: dict[str, Any]

    # {"step": "searching", "message": "...", "round": 1}; failures are recorded, not hidden
    errors: Annotated[list[dict[str, Any]], operator.add]


def initial_state(question: str, depth: Depth = "standard", research_id: str | None = None) -> ResearchState:
    return ResearchState(
        research_id=research_id or str(uuid.uuid4()),
        question=question.strip(),
        status="pending",
        plan={},
        round_number=0,
        pending_queries=[],
        used_queries=[],
        search_results=[],
        sources=[],
        pages=[],
        evidence=[],
        quality={},
        gap_analysis={},
        missing_questions=[],
        is_sufficient=False,
        depth=depth,
        limits=limits_for_depth(depth),
        started_at=time.time(),
        llm_calls=0,
        search_calls=0,
        stop_reason=None,
        report={},
        errors=[],
    )


def error_entry(step: str, message: str, round_number: int | None = None, **details: Any) -> dict[str, Any]:
    return {"step": step, "message": message, "round": round_number, **details}


def source_id(number: int) -> str:
    return f"S{number:02d}"


def evidence_id(number: int) -> str:
    return f"E{number:03d}"


def canonical_source_id(raw: str) -> str | None:
    """Turn what a model writes ('S1', 's01', '[S01]') into the stored form 'S01'. None if it isn't an ID."""
    match = re.fullmatch(r"\s*\[?\s*[Ss]0*(\d+)\s*\]?\s*", raw)
    return source_id(int(match.group(1))) if match else None


def id_number(identifier: str) -> int:
    """Numeric part of S01 or E001, for sorting."""
    return int(identifier[1:])


def to_json(state: ResearchState) -> str:
    """Serialize a state. Raises TypeError if a node put a non-JSON value in it."""
    return json.dumps(state, ensure_ascii=False)


def from_json(data: str) -> ResearchState:
    return ResearchState(**json.loads(data))
