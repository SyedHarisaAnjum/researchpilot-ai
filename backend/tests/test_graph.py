"""Research graph: node transitions, bounded loops, no repeated searches or reads."""

from app.agent.graph import run_research, stream_research
from app.agent.state import initial_state, to_json
from app.schemas import SearchQueries
from app.services.llm_service import LLMError
from app.services.search_service import SearchQuotaError
from tests.fakes import gap

LOOP = ["search_web", "read_pages", "extract_evidence", "evaluate_gaps"]


async def node_order(question="Compare free LLM APIs", depth="standard"):
    order, final = [], None
    async for node, _update, state in stream_research(initial_state(question, depth)):
        order.append(node)
        final = state
    return order, final


async def test_one_round_when_evidence_is_sufficient(services):
    order, state = await node_order()
    assert order == ["plan_research", *LOOP, "finalize_report"]
    assert state["status"] == "completed" and state["report"]
    to_json(state)  # the whole state stays serializable


async def test_more_rounds_while_gaps_remain_bounded_by_the_round_limit(services):
    services.llm.handlers["GapAnalysis"] = gap(False)
    order, state = await node_order(depth="standard")
    rounds = state["limits"]["max_rounds"]
    assert order == ["plan_research", *LOOP, *(["plan_next_round", *LOOP] * (rounds - 1)), "finalize_report"]
    assert state["round_number"] == rounds
    assert state["report"]["partial"] is True
    assert "Round limit" in state["report"]["stop_reason"]


async def test_quick_depth_runs_one_round(services):
    services.llm.handlers["GapAnalysis"] = gap(False)
    order, state = await node_order(depth="quick")
    assert order.count("search_web") == 1 and state["round_number"] == 1


async def test_queries_are_never_repeated_and_urls_never_reread(services):
    services.llm.handlers["GapAnalysis"] = gap(False)
    # The model keeps suggesting an old query; it must be dropped.
    services.llm.handlers["SearchQueries"] = lambda _p: SearchQueries(
        queries=["free llm api tool calling", f"new angle {services.llm.calls['SearchQueries']}"]
    )
    _, state = await node_order()
    queries = services.search.queries
    assert len(queries) == len({q.lower() for q in queries})
    assert len(services.reader.urls) == len(set(services.reader.urls))
    assert state["search_calls"] <= state["limits"]["max_total_searches"]
    assert len(state["sources"]) <= state["limits"]["max_pages_per_research"]


async def test_planning_failure_ends_the_run(services):
    services.llm.handlers["ResearchPlan"] = LLMError("bad model name")
    order, state = await node_order()
    assert order == ["plan_research"]
    assert state["status"] == "failed"
    assert "bad model name" in state["errors"][0]["message"]


async def test_temporary_planning_failure_uses_a_fallback_plan(services):
    from app.services.llm_service import LLMTimeoutError

    services.llm.handlers["ResearchPlan"] = LLMTimeoutError("The model did not respond in time")
    order, state = await node_order("Compare free LLM APIs")
    assert order[:2] == ["plan_research", "search_web"]
    assert state["plan"]["search_queries"] == ["Compare free LLM APIs"]
    assert state["status"] == "completed" and state["report"]
    assert "researching the question directly" in state["errors"][0]["message"]


async def test_model_call_budget_is_respected(services, monkeypatch):
    services.llm.handlers["GapAnalysis"] = gap(False)
    state = initial_state("Compare free LLM APIs", "standard")
    state["limits"]["max_llm_calls"] = 8
    async for _node, _update, final in stream_research(state):
        pass
    assert services.llm.total_calls <= 8
    assert final["report"]["partial"] is True and "Model call limit" in final["report"]["stop_reason"]


async def test_search_quota_stops_research_with_a_partial_report(services):
    for query in ("free llm api tool calling", "free llm api rate limits"):
        services.search.errors[query] = SearchQuotaError("Search provider quota is used up")
    _, state = await node_order()
    assert state["status"] == "completed"
    assert state["report"]["partial"] is True
    assert "quota" in state["report"]["stop_reason"]
    assert any("quota" in e["message"] for e in state["errors"])


async def test_blocked_pages_are_recorded_and_research_continues(services):
    services.reader.blocked = {"site0"}
    _, state = await node_order()
    blocked = [s for s in state["sources"] if s["fetch_status"] == "blocked"]
    assert blocked and all(s["source_id"] not in {e["source_id"] for e in state["evidence"]} for s in blocked)
    assert any(e["step"] == "reading" for e in state["errors"])
    assert state["status"] == "completed"


async def test_run_research_wrapper(services):
    state = await run_research("Compare free LLM APIs", depth="quick")
    assert state["depth"] == "quick" and state["status"] == "completed"
