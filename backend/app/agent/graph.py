"""The research workflow.

START -> plan_research -> search_web -> read_pages -> extract_evidence -> evaluate_gaps
                                ^                                             |
                                |              more research needed           |
                                +------------- plan_next_round <--------------+
                                                                              | enough evidence
                                                                              | or budget used up
                                                                  finalize_report -> END
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agent.nodes import (
    extract_evidence,
    evaluate_gaps,
    finalize_report,
    plan_next_round,
    plan_research,
    read_pages,
    research_stop_reason,
    search_web,
)
from app.agent.limits import Depth
from app.agent.state import ResearchState, initial_state
from app.utils.run_context import research_context

logger = logging.getLogger(__name__)


def route_after_plan(state: ResearchState) -> str:
    return END if state.get("status") == "failed" else "search_web"


def route_after_evaluation(state: ResearchState) -> str:
    return "finalize_report" if research_stop_reason(state) else "plan_next_round"


def route_after_next_round(state: ResearchState) -> str:
    return "search_web" if state.get("pending_queries") else "finalize_report"


def build_graph():
    builder = StateGraph(ResearchState)
    builder.add_node("plan_research", plan_research)
    builder.add_node("search_web", search_web)
    builder.add_node("read_pages", read_pages)
    builder.add_node("extract_evidence", extract_evidence)
    builder.add_node("evaluate_gaps", evaluate_gaps)
    builder.add_node("plan_next_round", plan_next_round)
    builder.add_node("finalize_report", finalize_report)

    builder.add_edge(START, "plan_research")
    builder.add_conditional_edges("plan_research", route_after_plan, {"search_web": "search_web", END: END})
    builder.add_edge("search_web", "read_pages")
    builder.add_edge("read_pages", "extract_evidence")
    builder.add_edge("extract_evidence", "evaluate_gaps")
    builder.add_conditional_edges(
        "evaluate_gaps",
        route_after_evaluation,
        {"plan_next_round": "plan_next_round", "finalize_report": "finalize_report"},
    )
    builder.add_conditional_edges(
        "plan_next_round",
        route_after_next_round,
        {"search_web": "search_web", "finalize_report": "finalize_report"},
    )
    builder.add_edge("finalize_report", END)
    return builder.compile()


research_graph = build_graph()


def _recursion_limit(state: ResearchState) -> int:
    # Each round is 5 steps, so this only trips if routing is broken.
    return 10 + 6 * state["limits"]["max_rounds"]


async def stream_research(state: ResearchState) -> AsyncIterator[tuple[str, dict[str, Any], ResearchState]]:
    """Run the graph, yielding (node name, that node's update, full state after it) after each node."""
    node = ""
    update: dict[str, Any] = {}
    async for mode, chunk in research_graph.astream(
        state, config={"recursion_limit": _recursion_limit(state)}, stream_mode=["updates", "values"]
    ):
        if mode == "updates":
            node, update = next(iter(chunk.items()))
        elif node:
            yield node, update or {}, chunk


async def run_research(question: str, depth: Depth = "standard", research_id: str | None = None) -> ResearchState:
    state = initial_state(question, depth, research_id)
    with research_context(state["research_id"]):
        logger.info("Research %s started (%s): %r", state["research_id"], depth, state["question"])
        final = await research_graph.ainvoke(state, config={"recursion_limit": _recursion_limit(state)})
    logger.info(
        "Research %s finished: status=%s rounds=%s llm_calls=%s searches=%s sources=%s evidence=%s",
        final["research_id"], final["status"], final["round_number"], final["llm_calls"],
        final["search_calls"], len(final["sources"]), len(final["evidence"]),
    )
    return final
