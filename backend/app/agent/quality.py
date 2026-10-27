"""Evidence checks done in code, so the LLM never grades its own work unchecked.

- Excerpts must appear in the page they were taken from, or the evidence is dropped.
- Coverage of each subquestion is counted from the evidence records.
- Evidence only comes from pages that were actually read, never from search snippets.
"""

import re
from typing import Any

from app.agent.state import id_number

SUPPORTED, WEAK, MISSING = "SUPPORTED", "WEAK", "MISSING"

_QUOTE_CHARS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def normalize_text(text: str) -> str:
    return " ".join(text.translate(_QUOTE_CHARS).lower().split())


def quote_in_content(quote: str, content: str) -> bool:
    """True if the quote appears in the page. Allows '...' where the model skipped words."""
    content = normalize_text(content)
    quote = normalize_text(quote).strip(" .\"'")
    if len(quote) < 8:
        return False
    if quote in content:
        return True
    parts = [p.strip(" .\"'") for p in re.split(r"\.\.\.|…", quote)]
    parts = [p for p in parts if len(p) >= 15]
    return bool(parts) and all(p in content for p in parts)


def match_subquestion(text: str, subquestions: list[str]) -> str | None:
    key = normalize_text(text).rstrip("?")
    for subquestion in subquestions:
        if normalize_text(subquestion).rstrip("?") == key:
            return subquestion
    return None


def assess_evidence(
    subquestions: list[str], evidence: list[dict[str, Any]], sources: list[dict[str, Any]]
) -> dict[str, Any]:
    """Coverage per subquestion.

    SUPPORTED: at least two sources, or one high-confidence source.
    WEAK:      some evidence, but less than that.
    MISSING:   no evidence at all.
    """
    coverage = []
    for subquestion in subquestions:
        items = [e for e in evidence if e.get("subquestion") == subquestion]
        cited = sorted({e["source_id"] for e in items}, key=id_number)
        high = sum(1 for e in items if e.get("confidence") == "high")
        if not items:
            status = MISSING
        elif len(cited) >= 2 or high >= 1:
            status = SUPPORTED
        else:
            status = WEAK
        coverage.append(
            {"subquestion": subquestion, "status": status, "evidence": len(items), "sources": cited, "high_confidence": high}
        )

    return {
        "coverage": coverage,
        "missing": [c["subquestion"] for c in coverage if c["status"] == MISSING],
        "weak": [c["subquestion"] for c in coverage if c["status"] == WEAK],
        "evidence_count": len(evidence),
        "pages_read": sum(1 for s in sources if s.get("fetch_status") == "success"),
        "pages_failed": sum(1 for s in sources if s.get("fetch_status") != "success"),
    }


def coverage_summary(quality: dict[str, Any]) -> str:
    lines = [
        f"- [{c['status']}] {c['subquestion']}: {c['evidence']} evidence items from "
        f"{len(c['sources'])} sources ({', '.join(c['sources']) or 'none'}), {c['high_confidence']} high-confidence"
        for c in quality["coverage"]
    ]
    lines.append(
        f"Pages read successfully: {quality['pages_read']}; pages that could not be read: {quality['pages_failed']}. "
        "Search snippets are not used as evidence."
    )
    return "\n".join(lines)
