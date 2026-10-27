"""Evidence keeps the right source; every citation in the report resolves to a saved source."""

import re

from app.agent.graph import run_research
from app.agent.quality import quote_in_content
from app.agent.state import canonical_source_id
from app.schemas import (
    ComparisonRow,
    ComparisonTable,
    DraftFinding,
    Evidence,
    EvidenceExtraction,
    ReportDraft,
    Statement,
    SupportCheck,
    SupportVerdict,
)
from app.services.llm_service import LLMError
from tests.fakes import claim_ids_in, default_evidence, source_id_in


def cited_ids(report) -> set[str]:
    ids = set(re.findall(r"\[(S\d+)\]", report["summary"]))
    for section in report["findings"] + [{"statements": report["conclusions"], "source_ids": []}]:
        ids |= set(section.get("source_ids", []))
        ids |= {sid for s in section["statements"] for sid in s["source_ids"]}
    for row in (report["comparison"] or {}).get("rows", []):
        ids |= set(row["source_ids"])
    return ids


async def test_evidence_keeps_its_own_source_id(services):
    # The model claims every item came from S99; the code must use the page it actually read.
    def wrong_ids(prompt):
        result = default_evidence(prompt)
        for item in result.evidence:
            item.source_id = "S99"
        return result

    services.llm.handlers["EvidenceExtraction"] = wrong_ids
    state = await run_research("Compare free LLM APIs")
    sources = {s["source_id"]: s for s in state["sources"]}
    pages = {p["source_id"]: p["content"] for p in state["pages"]}
    assert state["evidence"]
    for item in state["evidence"]:
        assert item["source_id"] in sources
        assert item["source_url"] == (sources[item["source_id"]]["final_url"] or sources[item["source_id"]]["url"])
        assert quote_in_content(item["supporting_excerpt"], pages[item["source_id"]])
    assert [e["evidence_id"] for e in state["evidence"]] == [f"E{i:03d}" for i in range(1, len(state["evidence"]) + 1)]


async def test_invented_quotes_are_discarded(services):
    def invented(prompt):
        sid = source_id_in(prompt)
        return EvidenceExtraction(relevant=True, evidence=[
            Evidence(source_id=sid, subquestion="x", claim="Unlimited free usage", quote="usage is unlimited forever", confidence="high"),
            Evidence(source_id=sid, subquestion="x", claim="15 RPM", quote="15 requests per minute", confidence="high"),
        ])

    services.llm.handlers["EvidenceExtraction"] = invented
    state = await run_research("Compare free LLM APIs")
    assert all("unlimited" not in e["claim"].lower() for e in state["evidence"])
    assert any("Discarded 1 evidence" in e["message"] for e in state["errors"])


async def test_every_citation_resolves_to_a_saved_source(services):
    services.llm.handlers["ReportDraft"] = lambda _p: ReportDraft(
        title="T",
        summary="Fact [S1], bad [S99], list [S01, S02]. See https://evil.example/x",
        findings=[DraftFinding(heading="H", statements=[
            Statement(text="A fact [S02].", kind="fact", source_ids=["s1"]),
            Statement(text="Only a fake source.", kind="fact", source_ids=["S42"]),
            Statement(text="Opinion.", kind="interpretation", source_ids=[]),
        ])],
        comparison=ComparisonTable(title="C", columns=["A", "B"], rows=[
            ComparisonRow(cells=["x", "y"], source_ids=["S01", "S77"]), ComparisonRow(cells=["z"], source_ids=[]),
        ]),
        conclusions=[Statement(text="Conclusion.", kind="fact", source_ids=["S03"])],
        limitations=[],
    )
    state = await run_research("Compare free LLM APIs")
    report = state["report"]
    saved = {s["source_id"]: s for s in state["sources"]}
    references = {r["source_id"]: r for r in report["sources"]}

    assert cited_ids(report) <= set(references) <= set(saved)
    for sid, ref in references.items():
        assert ref["url"] == (saved[sid]["final_url"] or saved[sid]["url"])
        assert ref["retrieved_at"] == saved[sid]["retrieved_at"]
    assert "S99" not in report["summary"] and "evil.example" not in report["summary"]
    statements = {s["text"]: s for s in report["findings"][0]["statements"]}
    assert statements["A fact."]["source_ids"] == ["S01", "S02"]
    assert statements["Only a fake source."]["verification"] == "no_citation"
    assert any("Only a fake source." in c for c in report["flagged_claims"])
    assert report["comparison"]["rows"][1]["cells"] == ["z", "unknown"]
    assert any("unknown sources" in e["message"] for e in state["errors"])


async def test_unsupported_claims_are_flagged_and_left_out(services):
    services.llm.handlers["SupportCheck"] = lambda prompt: SupportCheck(verdicts=[
        SupportVerdict(claim_id=i, verdict="not_supported" if i == "F1.1" else "supported", reason="excerpt says otherwise")
        for i in claim_ids_in(prompt)
    ])
    report = (await run_research("Compare free LLM APIs"))["report"]
    assert report["verification"]["not_supported"] == 1
    shown = " ".join(f["content"] for f in report["findings"])
    assert "Gemini Flash allows 15 requests per minute" not in shown
    assert any("excerpt says otherwise" in c for c in report["flagged_claims"])


async def test_report_failure_falls_back_to_verified_evidence(services):
    services.llm.handlers["ReportDraft"] = LLMError("timeout")
    state = await run_research("Compare free LLM APIs")
    report = state["report"]
    assert "could not be generated" in report["summary"]
    evidence_claims = {e["claim"] for e in state["evidence"]}
    assert {s["text"] for f in report["findings"] for s in f["statements"]} <= evidence_claims


def test_canonical_source_ids():
    assert [canonical_source_id(x) for x in ("S1", "s01", "[S3]", " S10 ", "E001", "source")] == ["S01", "S01", "S03", "S10", None, None]
