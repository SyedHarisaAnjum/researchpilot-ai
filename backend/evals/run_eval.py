"""Repeatable evaluation of the research agent over evals/questions.json.

Run from the backend/ folder:

    python -m evals.run_eval --mode mock                      # no API calls: checks the pipeline and metrics
    python -m evals.run_eval --mode mock --fault-rate 0.3     # reliability under injected failures
    python -m evals.run_eval --mode live --limit 2            # real Gemini + Tavily on 2 questions
    python -m evals.run_eval --mode live --ids F1 C2 R1       # chosen questions
    python -m evals.run_eval --mode live                      # all 20 (roughly 200+ Gemini calls at quick depth)

Results go to evals/results/<timestamp>_<mode>/: config.json, results.jsonl, summary.json,
summary.md and each report's Markdown in reports/.

Metrics (per question, then averaged per category):
  relevance           LLM judge score 1-5 (live only); judged on substance, not length or tone
  key_terms           share of expected key-term groups that appear in the report
  citation_coverage   factual statements with at least one valid citation / all factual statements
  citation_support    (supported + 0.5 * partly supported) / factual statements checked against sources
  completeness        plan subquestions with any verified evidence / all subquestions
  freshness           time-sensitive questions: cited sources published within 12 months / cited sources
  uncertainty         uncertain questions: does the report acknowledge gaps or conflicting evidence?
  efficiency          model calls, searches, pages, evidence items and seconds
  reliability         did the job complete with a report? which steps recorded errors?
"""

import argparse
import asyncio
import hashlib
import json
import os
import random
import statistics
import sys
import tempfile
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

BACKEND_DIR = Path(__file__).resolve().parent.parent
DATASET = Path(__file__).resolve().parent / "questions.json"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
sys.path.insert(0, str(BACKEND_DIR))

FRESH_DAYS = 365
UNCERTAINTY_WORDS = ("uncertain", "conflict", "disagree", "mixed", "limited evidence", "not found", "unknown",
                     "could not", "insufficient", "forecast", "projection", "estimate", "unclear")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["mock", "live"], default="mock",
                        help="mock: fake LLM/search/reader (free); live: real Gemini and Tavily (uses quota)")
    parser.add_argument("--depth", choices=["quick", "standard", "deep"], default="quick")
    parser.add_argument("--ids", nargs="*", help="only these question IDs, e.g. F1 C2")
    parser.add_argument("--category", choices=["factual", "comparative", "recent", "uncertain"])
    parser.add_argument("--limit", type=int, help="only the first N selected questions")
    parser.add_argument("--fault-rate", type=float, default=0.0,
                        help="probability of injecting a failure into each page read, search and model call")
    parser.add_argument("--seed", type=int, default=7, help="random seed for fault injection")
    parser.add_argument("--no-judge", action="store_true", help="skip the LLM relevance judge in live mode")
    parser.add_argument("--pause", type=float, default=5.0, help="seconds between questions in live mode (rate limits)")
    return parser.parse_args()


# --- Fault injection: wraps real or fake services and fails a share of calls ---


class FaultyReader:
    def __init__(self, inner, rate: float, rng: random.Random):
        self.inner, self.rate, self.rng = inner, rate, rng

    async def read_many(self, urls):
        from app.schemas import PageContent

        failed = {u for u in urls if self.rng.random() < self.rate}
        pages = {p.url: p for p in await self.inner.read_many([u for u in urls if u not in failed])} if len(failed) < len(urls) else {}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        return [
            PageContent(url=u, status="blocked", error="Injected fault: HTTP 403", retrieved_at=now) if u in failed else pages[u]
            for u in urls
        ]


class FaultySearch:
    def __init__(self, inner, rate: float, rng: random.Random):
        self.inner, self.rate, self.rng = inner, rate, rng

    async def search(self, query, max_results=None):
        from app.services.search_service import SearchError

        if self.rng.random() < self.rate:
            raise SearchError("Injected fault: search provider error (503)")
        return await self.inner.search(query, max_results)


class FaultyLLM:
    def __init__(self, inner, rate: float, rng: random.Random):
        self.inner, self.rate, self.rng = inner, rate, rng

    async def generate_structured(self, prompt, schema, **kwargs):
        from app.services.llm_service import LLMTimeoutError

        # A temporary failure that outlasted the service's own retries.
        if self.rng.random() < self.rate:
            raise LLMTimeoutError("Injected fault: the model did not respond in time")
        return await self.inner.generate_structured(prompt, schema, **kwargs)


def install_services(mode: str, fault_rate: float, seed: int):
    """Point the agent at the services to evaluate. Returns the LLM used for the judge (live only)."""
    from app.agent import nodes, planner
    from app.services import report_service

    if mode == "mock":
        from tests.fakes import FakeLLM, FakeReader, FakeSearch

        llm, search, reader = FakeLLM(), FakeSearch(), FakeReader()
    else:
        from app.services.llm_service import get_llm_service
        from app.services.search_service import get_search_service
        from app.services.web_reader import get_web_reader

        llm, search, reader = get_llm_service(), get_search_service(), get_web_reader()

    judge = llm if mode == "live" else None
    if fault_rate:
        rng = random.Random(seed)
        llm, search, reader = FaultyLLM(llm, fault_rate, rng), FaultySearch(search, fault_rate, rng), FaultyReader(reader, fault_rate, rng)
    nodes.get_llm_service = planner.get_llm_service = report_service.get_llm_service = lambda: llm
    nodes.get_search_service = lambda: search
    nodes.get_web_reader = lambda: reader
    return judge


# --- Relevance judge ---

JUDGE_INSTRUCTION = """You evaluate research reports. Judge substance only.

- Do not reward length, formatting or a confident tone. A short report that answers correctly, with
  appropriate caveats, deserves a higher score than a long one that dodges the question.
- relevance (1-5): 5 = directly and fully answers the question; 3 = partly answers it or answers a
  nearby question; 1 = does not answer it.
- answers_question: true if a reader gets a usable answer to the question as asked.
- handles_uncertainty: "yes" if the report clearly states gaps, conflicting evidence or uncertainty
  where the evidence calls for it; "no" if it presents uncertain things as settled; "not_applicable"
  if the evidence is clear-cut.
- main_gaps: what an expert would say is missing or wrong (at most 3, empty if none).
- Judge only the report text against the question and the expected behaviour. Do not add facts."""


def judgement_schema():
    from pydantic import BaseModel, Field

    class Judgement(BaseModel):
        relevance: int = Field(ge=1, le=5)
        answers_question: bool
        handles_uncertainty: Literal["yes", "no", "not_applicable"]
        main_gaps: list[str] = Field(max_length=3)
        reasoning: str

    return Judgement


async def judge_report(judge, question: dict[str, Any], report: dict[str, Any]) -> dict[str, Any] | None:
    if judge is None or not report:
        return None
    prompt = (
        f"Question: {question['question']}\n\nExpected behaviour: {question['expected_behavior']}\n\n"
        f"Report (Markdown):\n<report>\n{report['markdown'][:15000]}\n</report>"
    )
    try:
        result = await judge.generate_structured(prompt, judgement_schema(), system_instruction=JUDGE_INSTRUCTION)
        return result.model_dump()
    except Exception as exc:  # the judge failing must not fail the evaluation
        return {"error": f"{type(exc).__name__}: {exc}"}


# --- Metrics ---


def ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def compute_metrics(question: dict[str, Any], state: dict[str, Any], judgement: dict[str, Any] | None) -> dict[str, Any]:
    report = state.get("report") or {}
    verification = Counter(report.get("verification") or {})
    statements = [s for f in report.get("findings", []) for s in f["statements"]] + report.get("conclusions", [])
    interpretations = sum(1 for s in statements if s["kind"] == "interpretation")
    facts = sum(verification.values()) - interpretations
    checked = verification["supported"] + verification["partially_supported"] + verification["not_supported"]
    text = (report.get("markdown") or "").lower()
    groups = question.get("key_terms", [])
    coverage = (state.get("quality") or {}).get("coverage", [])
    stats = report.get("stats") or {}
    errors = state.get("errors", [])

    metrics: dict[str, Any] = {
        "completed": state.get("status") == "completed" and bool(report),
        "partial": report.get("partial"),
        "stop_reason": report.get("stop_reason"),
        "relevance": judgement.get("relevance") if judgement and "relevance" in judgement else None,
        "answers_question": judgement.get("answers_question") if judgement else None,
        "key_terms": ratio(sum(any(t.lower() in text for t in g) for g in groups), len(groups)),
        "citation_coverage": ratio(facts - verification["no_citation"], facts),
        "citation_support": ratio(verification["supported"] + 0.5 * verification["partially_supported"], checked),
        "unsupported_claims": verification["not_supported"] + verification["no_citation"],
        "completeness": ratio(sum(1 for c in coverage if c["status"] != "MISSING"), len(coverage)),
        "well_supported_subquestions": ratio(sum(1 for c in coverage if c["status"] == "SUPPORTED"), len(coverage)),
        "unresolved_questions": len(report.get("unresolved_questions", [])),
        "llm_calls": stats.get("llm_calls", state.get("llm_calls")),
        "searches": stats.get("searches", state.get("search_calls")),
        "pages_attempted": stats.get("pages_attempted", len(state.get("sources", []))),
        "pages_read": stats.get("pages_read"),
        "evidence_items": stats.get("evidence_items", len(state.get("evidence", []))),
        "seconds": stats.get("elapsed_seconds"),
        "errors_by_step": dict(Counter(e["step"] for e in errors)),
        "fallback_report": "could not be generated" in (report.get("summary") or ""),
    }

    if question.get("time_sensitive"):
        today = date.today()
        refs = report.get("sources", [])
        recent = [r for r in refs if r.get("published_at") and (today - date.fromisoformat(r["published_at"][:10])).days <= FRESH_DAYS]
        metrics["freshness"] = ratio(len(recent), len(refs))
        metrics["undated_sources"] = ratio(sum(1 for r in refs if not r.get("published_at")), len(refs))
        currency = (state.get("gap_analysis") or {}).get("currency_concerns") or []
        notes = " ".join(report.get("limitations", [])).lower()
        metrics["currency_noted"] = bool(currency) or any(w in notes for w in ("out of date", "outdated", "change", "as of", "current"))

    if question["category"] == "uncertain":
        gap = state.get("gap_analysis") or {}
        signals = bool(report.get("unresolved_questions")) or bool(gap.get("conflicts")) or bool(report.get("partial"))
        signals = signals or any(w in (report.get("summary") or "").lower() for w in UNCERTAINTY_WORDS)
        metrics["uncertainty_acknowledged"] = signals
        if judgement and "handles_uncertainty" in judgement:
            metrics["judge_handles_uncertainty"] = judgement["handles_uncertainty"]
    return metrics


SUMMARY_METRICS = [
    "completed", "relevance", "key_terms", "citation_coverage", "citation_support", "completeness",
    "freshness", "uncertainty_acknowledged", "llm_calls", "searches", "pages_read", "evidence_items", "seconds",
]


def mean(values: list) -> float | None:
    numbers = [float(v) for v in values if isinstance(v, (int, float, bool)) and v is not None]
    return round(statistics.mean(numbers), 3) if numbers else None


def summarize(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups = {"all": rows}
    for row in rows:
        groups.setdefault(row["category"], []).append(row)
    return {
        name: {"questions": len(items), **{m: mean([r["metrics"].get(m) for r in items]) for m in SUMMARY_METRICS}}
        for name, items in groups.items()
    }


def markdown_summary(config: dict[str, Any], summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    def fmt(value):
        return "–" if value is None else (f"{value:.2f}" if isinstance(value, float) else str(value))

    lines = [
        f"# Evaluation {config['started_at']}",
        "",
        f"Mode **{config['mode']}**, depth **{config['depth']}**, model `{config['model']}`, fault rate {config['fault_rate']}, "
        f"dataset sha256 `{config['dataset_sha256'][:12]}`.",
        "",
        "## By category",
        "",
        "| Group | n | " + " | ".join(SUMMARY_METRICS) + " |",
        "|" + " --- |" * (len(SUMMARY_METRICS) + 2),
    ]
    for name, values in summary.items():
        lines.append(f"| {name} | {values['questions']} | " + " | ".join(fmt(values[m]) for m in SUMMARY_METRICS) + " |")
    lines += ["", "## By question", "", "| ID | completed | relevance | key terms | coverage | support | complete | calls | s | stop / error |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        m = row["metrics"]
        note = row.get("crash") or m.get("stop_reason") or ""
        lines.append(
            f"| {row['id']} | {fmt(m.get('completed'))} | {fmt(m.get('relevance'))} | {fmt(m.get('key_terms'))} | "
            f"{fmt(m.get('citation_coverage'))} | {fmt(m.get('citation_support'))} | {fmt(m.get('completeness'))} | "
            f"{fmt(m.get('llm_calls'))} | {fmt(m.get('seconds'))} | {note} |"
        )
    lines += ["", "Relevance comes from an LLM judge told to ignore length and tone; treat it as a signal, and read the reports in `reports/`."]
    return "\n".join(lines) + "\n"


async def run(args: argparse.Namespace) -> Path:
    from app.agent.graph import run_research
    from app.agent.limits import limits_for_depth
    from app.config import get_settings

    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    questions = dataset["questions"]
    if args.ids:
        questions = [q for q in questions if q["id"] in set(args.ids)]
    if args.category:
        questions = [q for q in questions if q["category"] == args.category]
    if args.limit:
        questions = questions[: args.limit]

    judge = install_services(args.mode, args.fault_rate, args.seed)
    if args.no_judge:
        judge = None

    started = datetime.now(timezone.utc)
    out = RESULTS_DIR / f"{started.strftime('%Y%m%dT%H%M%SZ')}_{args.mode}"
    (out / "reports").mkdir(parents=True)
    config = {
        "started_at": started.isoformat(timespec="seconds"),
        "mode": args.mode,
        "depth": args.depth,
        "limits": limits_for_depth(args.depth),
        "model": get_settings().gemini_model if args.mode == "live" else "fake",
        "fault_rate": args.fault_rate,
        "seed": args.seed,
        "judge": judge is not None,
        "questions": [q["id"] for q in questions],
        "dataset_version": dataset.get("version"),
        "dataset_sha256": hashlib.sha256(DATASET.read_bytes()).hexdigest(),
    }
    (out / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    rows = []
    with (out / "results.jsonl").open("w", encoding="utf-8") as results_file:
        for index, question in enumerate(questions, 1):
            print(f"[{index}/{len(questions)}] {question['id']} {question['question']}", flush=True)
            began = time.perf_counter()
            row: dict[str, Any] = {"id": question["id"], "category": question["category"], "question": question["question"]}
            try:
                state = await run_research(question["question"], depth=args.depth)
                judgement = await judge_report(judge, question, state.get("report") or {})
                row["metrics"] = compute_metrics(question, state, judgement)
                row["judgement"] = judgement
                report = state.get("report") or {}
                row["report"] = {k: report.get(k) for k in ("title", "summary", "limitations", "flagged_claims", "verification")}
                row["errors"] = [e["message"] for e in state.get("errors", [])]
                (out / "reports" / f"{question['id']}.md").write_text(report.get("markdown") or "(no report)\n", encoding="utf-8")
            except Exception as exc:  # a crash is a reliability result, not a reason to stop
                row["crash"] = f"{type(exc).__name__}: {exc}"
                row["metrics"] = {"completed": False}
            row["wall_seconds"] = round(time.perf_counter() - began, 1)
            rows.append(row)
            results_file.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            results_file.flush()
            m = row["metrics"]
            print(f"    completed={m.get('completed')} relevance={m.get('relevance')} support={m.get('citation_support')} "
                  f"coverage={m.get('citation_coverage')} complete={m.get('completeness')} calls={m.get('llm_calls')} "
                  f"{row.get('crash') or ''}", flush=True)
            if args.mode == "live" and index < len(questions):
                await asyncio.sleep(args.pause)

    summary = summarize(rows)
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out / "summary.md").write_text(markdown_summary(config, summary, rows), encoding="utf-8")
    return out


def main() -> None:
    args = parse_args()
    if args.mode == "mock":
        # Never touch real keys or quota in mock mode.
        os.environ.update({"GEMINI_API_KEY": "mock", "TAVILY_API_KEY": "mock", "GEMINI_MODEL": "mock",
                           "DATABASE_URL": f"sqlite+aiosqlite:///{Path(tempfile.mkdtemp()) / 'eval.db'}"})
    import logging

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    out = asyncio.run(run(args))
    print(f"\nResults: {out}\n")
    print((out / "summary.md").read_text(encoding="utf-8").split("## By question")[0])


if __name__ == "__main__":
    main()
