"""Builds the final report from stored records and validates every citation in it.

1. The LLM drafts the report as statements, each marked fact or interpretation, citing source IDs.
2. Citations are checked: unknown source IDs are removed, URLs not in the source records are removed.
3. A second LLM pass checks each fact against the excerpts of the sources it cites. It is an aid,
   not a guarantee: verdicts are shown alongside the claims, and unsupported facts are flagged.
4. Methodology, references and dates come from stored records, never from the LLM.
"""

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.agent import prompts
from app.agent.quality import normalize_text
from app.agent.state import ResearchState, canonical_source_id, error_entry, id_number
from app.schemas import (
    ComparisonTable,
    DraftFinding,
    FinalReport,
    Reference,
    ReportDraft,
    ReportFinding,
    Statement,
    SupportCheck,
    VerifiedStatement,
)
from app.services.llm_service import LLMError, get_llm_service
from app.services.search_service import normalize_url
from app.utils.run_context import tokens_used

logger = logging.getLogger(__name__)

CITATION = re.compile(r"\[\s*([Ss]\d+(?:\s*[,;]\s*[Ss]\d+)*)\s*\]")
MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
BARE_URL = re.compile(r"https?://[^\s)\]>\"']+")
SUPPORT_BATCH_SIZE = 15
MAX_EXCERPTS_PER_CLAIM = 6
FLAGGED = {"not_supported", "no_citation"}
VERDICT_LABELS = {
    "supported": "",
    "partially_supported": " *(only partly supported by the cited sources)*",
    "unchecked": " *(not verified)*",
}


@dataclass
class ReportResult:
    report: dict[str, Any]
    llm_calls: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)


class CitationValidator:
    """Checks source IDs and URLs against the stored source records."""

    def __init__(self, sources: list[dict[str, Any]]):
        self.sources = {s["source_id"]: s for s in sources}
        self.known_urls = {normalize_url(u) for s in sources for u in (s["url"], s.get("final_url")) if u}
        self.unknown_ids: set[str] = set()
        self.removed_urls: set[str] = set()

    def valid_ids(self, raw_ids: list[str]) -> list[str]:
        valid: list[str] = []
        for raw in raw_ids:
            sid = canonical_source_id(raw)
            if sid in self.sources:
                if sid not in valid:
                    valid.append(sid)
            else:
                self.unknown_ids.add(raw.strip())
        return sorted(valid, key=id_number)

    def clean_urls(self, text: str) -> str:
        """Keep only URLs that match a saved source record."""

        def link(match: re.Match) -> str:
            if normalize_url(match.group(2)) in self.known_urls:
                return match.group(0)
            self.removed_urls.add(match.group(2))
            return match.group(1)

        def bare(match: re.Match) -> str:
            url = match.group(0).rstrip(".,;:")
            if normalize_url(url) in self.known_urls:
                return match.group(0)
            self.removed_urls.add(url)
            return ""

        return BARE_URL.sub(bare, MARKDOWN_LINK.sub(link, text))

    def split_citations(self, text: str) -> tuple[str, list[str]]:
        """Remove [S01]-style markers from text and return them separately."""
        found: list[str] = []

        def collect(match: re.Match) -> str:
            found.extend(re.split(r"\s*[,;]\s*", match.group(1)))
            return ""

        text = CITATION.sub(collect, text)
        return re.sub(r"\s+([.,;:!?])", r"\1", " ".join(text.split())), found

    def normalize_inline(self, text: str) -> str:
        """Rewrite inline citations to the stored form ([S1] -> [S01]) and drop unknown ones."""

        def rewrite(match: re.Match) -> str:
            ids = self.valid_ids(re.split(r"\s*[,;]\s*", match.group(1)))
            return "".join(f"[{sid}]" for sid in ids)

        text = CITATION.sub(rewrite, self.clean_urls(text))
        return re.sub(r"\s+([.,;:!?])", r"\1", " ".join(text.split()))


def _verify_statements(
    statements: list[Statement], validator: CitationValidator, prefix: str
) -> list[VerifiedStatement]:
    verified = []
    for i, statement in enumerate(statements, start=1):
        text, inline_ids = validator.split_citations(validator.clean_urls(statement.text))
        if not text:
            continue
        ids = validator.valid_ids(statement.source_ids + inline_ids)
        if statement.kind == "fact" and not ids:
            verification, note = "no_citation", "No valid source was cited for this fact"
        else:
            verification, note = "unchecked", None
        verified.append(
            VerifiedStatement(
                statement_id=f"{prefix}{i}", text=text, kind=statement.kind, source_ids=ids,
                verification=verification, verification_note=note,
            )
        )
    return verified


def _best_excerpts(text: str, source_ids: list[str], evidence_by_source: dict[str, list[dict[str, Any]]]) -> list[dict[str, str]]:
    """The cited sources' excerpts that share the most words with the claim."""
    claim_words = set(normalize_text(text).split())
    candidates = [
        {"source_id": sid, "excerpt": e["supporting_excerpt"], "claim": e["claim"]}
        for sid in source_ids
        for e in evidence_by_source.get(sid, [])
    ]
    candidates.sort(
        key=lambda c: len(claim_words & set(normalize_text(c["excerpt"] + " " + c["claim"]).split())), reverse=True
    )
    return [{"source_id": c["source_id"], "excerpt": c["excerpt"]} for c in candidates[:MAX_EXCERPTS_PER_CLAIM]]


async def _support_check(
    statements: list[VerifiedStatement],
    evidence_by_source: dict[str, list[dict[str, Any]]],
    max_calls: int,
    result: ReportResult,
    limits: dict[str, Any],
) -> None:
    """Second pass: does each cited excerpt actually support its fact? Updates statements in place."""
    facts = [s for s in statements if s.kind == "fact" and s.source_ids]
    batches = [facts[i : i + SUPPORT_BATCH_SIZE] for i in range(0, len(facts), SUPPORT_BATCH_SIZE)]
    llm = get_llm_service()
    max_tokens = limits.get("max_total_tokens")
    for batch in batches:
        tokens = tokens_used()
        over_tokens = tokens is not None and max_tokens and tokens >= max_tokens
        if max_calls <= 0 or over_tokens:
            reason = "token limit reached" if over_tokens else "model call limit reached"
            for s in batch:
                s.verification_note = f"Not checked: {reason}"
            continue
        max_calls -= 1
        result.llm_calls += 1
        claims = [
            {"claim_id": s.statement_id, "text": s.text, "excerpts": _best_excerpts(s.text, s.source_ids, evidence_by_source)}
            for s in batch
        ]
        try:
            check = await llm.generate_structured(
                prompts.build_support_prompt(claims), SupportCheck, system_instruction=prompts.SUPPORT_INSTRUCTION
            )
        except LLMError as exc:
            result.errors.append(error_entry("verifying", f"Support check failed for {len(batch)} claims: {exc}"))
            for s in batch:
                s.verification_note = "Not checked: support check failed"
            continue
        verdicts = {v.claim_id: v for v in check.verdicts}
        for s in batch:
            if verdict := verdicts.get(s.statement_id):
                s.verification, s.verification_note = verdict.verdict, verdict.reason
            else:
                s.verification_note = "Not checked: no verdict returned"


def _evidence_only_draft(state: ResearchState, reason: str) -> ReportDraft:
    """A draft built from verified evidence alone, used when the model can't write one."""
    by_subquestion: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in state["evidence"]:
        by_subquestion[item["subquestion"]].append(item)
    findings = [
        DraftFinding(
            heading=subquestion,
            statements=[Statement(text=e["claim"], kind="fact", source_ids=[e["source_id"]]) for e in items[:12]],
        )
        for subquestion, items in by_subquestion.items()
    ]
    summary = (
        f"A written summary could not be generated ({reason}). The verified evidence is listed below by subquestion."
        if findings
        else f"No verified evidence could be collected for this question ({reason})."
    )
    return ReportDraft(
        title=state["question"], summary=summary,
        findings=findings or [DraftFinding(heading="No findings", statements=[Statement(text="No evidence was collected.", kind="interpretation", source_ids=[])])],
        comparison=ComparisonTable(title="", columns=[], rows=[]), conclusions=[], limitations=[],
    )


def _comparison(table: ComparisonTable, validator: CitationValidator, flagged: list[str]) -> ComparisonTable | None:
    if not table.columns or not table.rows:
        return None
    width = len(table.columns)
    rows = []
    for row in table.rows:
        cells = [" ".join(validator.split_citations(validator.clean_urls(c))[0].split()) or "unknown" for c in row.cells]
        cells = (cells + ["unknown"] * width)[:width]
        ids = validator.valid_ids(row.source_ids + [i for c in row.cells for i in validator.split_citations(c)[1]])
        if not ids:
            flagged.append(f"Comparison row '{cells[0]}' has no supporting source")
        rows.append({"cells": cells, "source_ids": ids})
    return ComparisonTable(title=table.title or "Comparison", columns=table.columns, rows=rows)


def _statement_markdown(statement: VerifiedStatement) -> str:
    cites = "".join(f"[{sid}]" for sid in statement.source_ids)
    prefix = "*Interpretation:* " if statement.kind == "interpretation" else ""
    label = "" if statement.kind == "interpretation" else VERDICT_LABELS.get(statement.verification, "")
    return f"{prefix}{statement.text} {cites}".rstrip() + label


def _shown(statements: list[VerifiedStatement]) -> list[VerifiedStatement]:
    """Statements fit to show as findings; flagged ones are listed separately instead."""
    return [s for s in statements if s.verification not in FLAGGED]


def _methodology(state: ResearchState, verification: Counter, now: datetime) -> list[str]:
    sources = state["sources"]
    read = sum(1 for s in sources if s["fetch_status"] == "success")
    discarded = sum(e.get("discarded", 0) for e in state["errors"])
    facts_checked = sum(verification[k] for k in ("supported", "partially_supported", "not_supported"))
    return [
        f"Research carried out on {now.date().isoformat()} in {state['round_number']} round(s), "
        f"using {state['search_calls']} web searches: {', '.join(repr(q) for q in state['used_queries']) or 'none'}.",
        f"{len(state['search_results'])} unique search results were found. {len(sources)} pages were fetched: "
        f"{read} were read and {len(sources) - read} could not be read (blocked, unsupported or failed).",
        "Search snippets were not used as evidence; only text from pages that were read.",
        f"{len(state['evidence'])} evidence items were extracted. Each excerpt was checked to appear word for word "
        f"in its source page" + (f"; {discarded} items failed this check and were discarded." if discarded else "."),
        f"{facts_checked} factual statements were checked against the excerpts of the sources they cite by a second "
        "model pass. This check is an aid, not a guarantee of accuracy.",
        "Statements marked as interpretation are the model's own analysis and were not verified.",
    ]


def render_markdown(report: FinalReport) -> str:
    lines = [f"# {report.title}", "", f"**Research question:** {report.question}", ""]
    if report.partial:
        reason = f"{report.stop_reason}." if report.stop_reason else "Some questions remain open."
        lines += [f"> **Partial report.** {reason} See Limitations.", ""]
    lines += ["## Executive summary", "", report.summary, "", "## Methodology and scope", ""]
    lines += [f"- {m}" for m in report.methodology] + ["", "**Scope**", ""] + [f"- {s}" for s in report.scope] + [""]
    lines += ["## Detailed findings", ""]
    for finding in report.findings:
        lines += [f"### {finding.heading}", "", finding.content, ""]
    if report.comparison:
        table = report.comparison
        lines += [f"## {table.title}", "", "| " + " | ".join(table.columns + ["Sources"]) + " |",
                  "|" + " --- |" * (len(table.columns) + 1)]
        for row in table.rows:
            cells = [c.replace("|", "\\|") for c in row.cells] + [" ".join(f"[{s}]" for s in row.source_ids) or "none"]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    lines += ["## Conclusions", ""]
    lines += [f"- {_statement_markdown(s)}" for s in _shown(report.conclusions)] or ["- No conclusions could be drawn."]
    lines += ["", "## Limitations and unresolved questions", ""]
    lines += [f"- {item}" for item in report.limitations] or ["- None recorded."]
    if report.unresolved_questions:
        lines += ["", "**Unresolved questions:**"] + [f"- {q}" for q in report.unresolved_questions]
    if report.flagged_claims:
        lines += ["", "**Flagged claims** (left out of the findings, or shown without a source; do not rely on them):"]
        lines += [f"- {c}" for c in report.flagged_claims]
    lines += ["", "## References", ""]
    for ref in report.sources:
        title = (ref.title or ref.domain).replace("[", "(").replace("]", ")")
        lines.append(
            f"- [{ref.source_id} — {title}]({ref.url}) · {ref.domain} · "
            f"Published: {ref.published_at or 'unknown'} · Retrieved: {ref.retrieved_at}"
        )
    return "\n".join(lines).strip() + "\n"


async def build_report(state: ResearchState, stop_reason: str | None) -> ReportResult:
    result = ReportResult(report={})
    plan = state["plan"]
    now = datetime.now(timezone.utc)
    budget = state["limits"]["max_llm_calls"] - state["llm_calls"]
    missing = state.get("missing_questions", [])
    gap = state.get("gap_analysis") or {}
    concerns = gap.get("weak_evidence", []) + gap.get("conflicts", []) + gap.get("currency_concerns", [])

    # Only sources that produced verified evidence may be cited.
    evidence_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in state["evidence"]:
        evidence_by_source[item["source_id"]].append(item)
    citable = [s for s in state["sources"] if s["source_id"] in evidence_by_source]
    validator = CitationValidator(citable)

    # 1. Draft
    draft: ReportDraft | None = None
    if not state["evidence"]:
        reason = "no evidence was collected"
    elif budget <= 0:
        reason = "model call limit reached"
    else:
        budget -= 1
        result.llm_calls += 1
        try:
            draft = await get_llm_service().generate_structured(
                prompts.build_report_prompt(
                    state["question"], plan["research_goal"], plan["subquestions"],
                    state["evidence"], citable, missing, concerns,
                ),
                ReportDraft,
                system_instruction=prompts.REPORT_INSTRUCTION,
            )
        except LLMError as exc:
            reason = f"report generation failed: {exc}"
            result.errors.append(error_entry("writing", f"Report generation failed: {exc}"))
    if draft is None:
        draft = _evidence_only_draft(state, reason)

    # 2. Citation IDs and URLs
    flagged: list[str] = []
    summary = validator.normalize_inline(draft.summary)
    findings = [(f.heading, _verify_statements(f.statements, validator, f"F{i}.")) for i, f in enumerate(draft.findings, 1)]
    conclusions = _verify_statements(draft.conclusions, validator, "C")
    comparison = _comparison(draft.comparison, validator, flagged)
    all_statements = [s for _, statements in findings for s in statements] + conclusions

    # 3. Does each cited source support its claim?
    await _support_check(all_statements, evidence_by_source, budget, result, state["limits"])
    verification = Counter(s.verification for s in all_statements)

    for s in all_statements:
        if s.verification in FLAGGED:
            flagged.append(f"{s.text} ({s.verification_note or s.verification.replace('_', ' ')})")

    report_findings = []
    for heading, statements in findings:
        shown = _shown(statements)
        if not shown:
            continue
        report_findings.append(
            ReportFinding(
                heading=heading,
                content="\n".join(f"- {_statement_markdown(s)}" for s in shown),
                statements=statements,
                source_ids=sorted({sid for s in shown for sid in s.source_ids}, key=id_number),
            )
        )

    # 4. References from stored records: only sources the report actually cites
    cited_ids = set(re.findall(r"\[(S\d+)\]", summary))
    cited_ids |= {sid for f in report_findings for sid in f.source_ids}
    cited_ids |= {sid for s in _shown(conclusions) for sid in s.source_ids}
    cited_ids |= {sid for row in (comparison.rows if comparison else []) for sid in row.source_ids}
    references = [
        Reference(
            source_id=s["source_id"], title=s["title"], url=s["final_url"] or s["url"], domain=s["domain"],
            published_at=s["published_at"], retrieved_at=s["retrieved_at"],
        )
        for s in sorted(citable, key=lambda s: id_number(s["source_id"]))
        if s["source_id"] in cited_ids
    ]

    # Limitations added by code, so they appear even if the model leaves them out.
    # Partial when questions are still open, or a limit stopped research before it was judged complete.
    # (The reviewer sometimes says "not sufficient" yet lists nothing missing; that alone isn't partial.)
    partial = bool(missing) or (not state.get("is_sufficient") and stop_reason != "No open questions remain")
    failed_pages = sum(1 for s in state["sources"] if s["fetch_status"] != "success")
    limitations = list(draft.limitations)
    limitations += [f"Not answered with sufficient evidence: {q}" for q in missing]
    if partial and stop_reason:
        limitations.append(f"Research ended before all questions were answered: {stop_reason}.")
    if failed_pages:
        limitations.append(f"{failed_pages} source(s) could not be read (blocked, unsupported or failed) and were not used.")
    if flagged:
        limitations.append(f"{len(flagged)} claim(s) were removed or flagged because the cited sources did not support them.")
    if verification["unchecked"] - sum(1 for s in all_statements if s.kind == "interpretation"):
        limitations.append("Some factual statements could not be checked against their sources.")
    undated = [r.source_id for r in references if not r.published_at]
    if undated:
        limitations.append(f"No publication date was found for {', '.join(undated)}; only the retrieval date is known.")
    if validator.unknown_ids:
        result.errors.append(error_entry("verifying", f"Removed citations to unknown sources: {', '.join(sorted(validator.unknown_ids))}"))
    if validator.removed_urls:
        result.errors.append(error_entry("verifying", f"Removed URLs not in the source records: {', '.join(sorted(validator.removed_urls))}"))

    report = FinalReport(
        title=draft.title,
        question=state["question"],
        summary=summary,
        methodology=_methodology(state, verification, now),
        scope=[f"Research goal: {plan['research_goal']}"] + [f"Subquestion: {q}" for q in plan["subquestions"]],
        findings=report_findings,
        comparison=comparison,
        conclusions=conclusions,
        limitations=limitations,
        unresolved_questions=missing,
        flagged_claims=flagged,
        verification=dict(verification),
        sources=references,
        partial=partial,
        insufficient_evidence=not state["evidence"],
        stop_reason=stop_reason,
        stats={
            "rounds": state["round_number"],
            "searches": state["search_calls"],
            "pages_attempted": len(state["sources"]),
            "pages_read": len(state["sources"]) - failed_pages,
            "evidence_items": len(state["evidence"]),
            "llm_calls": state["llm_calls"] + result.llm_calls,
            "tokens": tokens_used() or 0,
            "elapsed_seconds": round((now.timestamp() - state["started_at"]), 1),
        },
        generated_at=now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        markdown="",
    )
    report.markdown = render_markdown(report)
    result.report = report.model_dump()
    return result
