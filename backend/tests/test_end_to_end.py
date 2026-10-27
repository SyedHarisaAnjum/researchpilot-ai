"""End to end: a question goes in through the API and a saved, cited report comes out.

The mocked test runs every time. The live test spends real quota (about 10 Gemini calls
and 3 searches) and only runs with RUN_LIVE_TESTS=1:

    RUN_LIVE_TESTS=1 pytest -m live -s
"""

import re
import time

import pytest

from app.agent.graph import run_research
from tests.conftest import signed_in_client


def test_question_produces_a_saved_cited_report(services):
    with signed_in_client() as client:
        job = client.post("/api/research", json={"question": "Compare free LLM APIs for agentic research", "depth": "quick"}).json()
        deadline = time.time() + 15
        while client.get(f"/api/research/{job['id']}").json()["status"] not in {"completed", "failed"}:
            assert time.time() < deadline
            time.sleep(0.05)
        report = client.get(f"/api/research/{job['id']}/report").json()["report"]
        sources = client.get(f"/api/research/{job['id']}/sources").json()

    saved_ids = {s["source_id"] for s in sources}
    assert report["title"] and report["summary"] and report["findings"]
    assert set(re.findall(r"\[(S\d+)\]", report["markdown"])) <= saved_ids
    assert "## References" in report["markdown"]
    assert report["verification"].get("supported", 0) > 0


@pytest.mark.live
async def test_live_quick_research():
    state = await run_research("What are the free-tier rate limits of the Gemini API?", depth="quick")
    report = state["report"]
    assert state["status"] == "completed", state["errors"]
    assert report["sources"], "no sources cited"
    checked = sum(report["verification"].get(k, 0) for k in ("supported", "partially_supported", "not_supported"))
    assert checked and report["verification"].get("supported", 0) / checked >= 0.5
    print("\n" + report["markdown"][:2000])
