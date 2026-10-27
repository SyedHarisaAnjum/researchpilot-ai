"""Fake LLM, search and reader with realistic, configurable behaviour."""

import asyncio
import re
from collections import Counter
from datetime import datetime, timezone

from app.utils.run_context import record_usage
from app.schemas import (
    ComparisonRow,
    ComparisonTable,
    DraftFinding,
    Evidence,
    EvidenceExtraction,
    GapAnalysis,
    PageContent,
    ReportDraft,
    ResearchPlan,
    SearchQueries,
    SearchResult,
    Statement,
    SupportCheck,
    SupportVerdict,
)

SUBQUESTIONS = ["Which free LLM APIs support tool calling?", "What are their free-tier rate limits?"]
PAGE_TEXT = (
    "Gemini Flash offers a free tier with 15 requests per minute. "
    "Tool calling is supported by the Gemini API. "
    "Groq provides 30 requests per minute on its free plan. "
) * 4


def source_id_in(prompt: str) -> str:
    return re.search(r'<source id="(S\d+)"', prompt).group(1)


def claim_ids_in(prompt: str) -> list[str]:
    return re.findall(r"^claim_id: (\S+)$", prompt, flags=re.M)


def default_plan(_prompt):
    return ResearchPlan(
        main_question="Compare free LLM APIs",
        subquestions=SUBQUESTIONS,
        search_queries=["free llm api tool calling", "free llm api rate limits"],
        research_goal="An evidence-backed comparison.",
    )


def default_evidence(prompt):
    sid = source_id_in(prompt)
    return EvidenceExtraction(
        relevant=True,
        evidence=[
            Evidence(source_id=sid, subquestion=SUBQUESTIONS[1], claim="Gemini Flash free tier: 15 RPM",
                     quote="Gemini Flash offers a free tier with 15 requests per minute", confidence="high"),
            Evidence(source_id=sid, subquestion=SUBQUESTIONS[0], claim="Gemini supports tool calling",
                     quote="Tool calling is supported by the Gemini API", confidence="high"),
        ],
    )


def gap(sufficient: bool, missing: list[str] | None = None):
    return GapAnalysis(
        answered_subquestions=SUBQUESTIONS, missing_questions=[] if sufficient else (missing or ["What are the daily caps?"]),
        weak_evidence=[], conflicts=[], currency_concerns=[], is_sufficient=sufficient, reasoning="test",
    )


def default_report(_prompt):
    return ReportDraft(
        title="Free LLM APIs",
        summary="Gemini Flash allows 15 RPM [S01] and supports tool calling [S1].",
        findings=[
            DraftFinding(heading="Limits", statements=[
                Statement(text="Gemini Flash allows 15 requests per minute.", kind="fact", source_ids=["S01"]),
                Statement(text="This suits small agents.", kind="interpretation", source_ids=[]),
            ]),
        ],
        comparison=ComparisonTable(title="", columns=[], rows=[]),
        conclusions=[Statement(text="Tool calling is supported by the Gemini API.", kind="fact", source_ids=["S01"])],
        limitations=[],
    )


def all_supported(prompt):
    return SupportCheck(verdicts=[SupportVerdict(claim_id=i, verdict="supported", reason="ok") for i in claim_ids_in(prompt)])


class FakeLLM:
    """Returns a canned result per output schema. Set handlers[schema_name] to a value,
    a function of the prompt, or an exception to raise."""

    def __init__(self):
        self.tokens_per_call = 0  # simulated usage, counted against the job's token budget
        self.calls: Counter[str] = Counter()
        self.prompts: list[tuple[str, str]] = []
        self.handlers = {
            "ResearchPlan": default_plan,
            "EvidenceExtraction": default_evidence,
            "GapAnalysis": gap(True),
            "SearchQueries": lambda _p: SearchQueries(queries=[f"follow up query {self.calls['SearchQueries']}"]),
            "ReportDraft": default_report,
            "SupportCheck": all_supported,
        }

    async def generate_structured(self, prompt, schema, *, system_instruction=None, **_):
        name = schema.__name__
        self.calls[name] += 1
        if self.tokens_per_call:
            record_usage(self.tokens_per_call, 0, 0)
        self.prompts.append((name, prompt))
        handler = self.handlers[name]
        result = handler(prompt) if callable(handler) else handler
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())


class FakeSearch:
    def __init__(self, results_per_query: int = 3):
        self.results_per_query = results_per_query
        self.queries: list[str] = []
        self.errors: dict[str, Exception] = {}  # query -> exception to raise

    async def search(self, query, max_results=None):
        self.queries.append(query)
        if query in self.errors:
            raise self.errors[query]
        slug = re.sub(r"\W+", "-", query.lower())
        return [
            SearchResult(title=f"{query} result {i}", url=f"https://site{i}.example.com/{slug}", snippet="snippet", provider="fake")
            for i in range(self.results_per_query)
        ]


class FakeReader:
    def __init__(self):
        self.urls: list[str] = []
        self.blocked: set[str] = set()  # substrings of URLs to report as blocked
        self.page_text = PAGE_TEXT
        self.delay = 0.0

    async def read_many(self, urls):
        self.urls.extend(urls)
        if self.delay:
            await asyncio.sleep(self.delay)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        pages = []
        for url in urls:
            if any(b in url for b in self.blocked):
                pages.append(PageContent(url=url, status="blocked", error="Site refused access (HTTP 403)", retrieved_at=now))
            else:
                pages.append(PageContent(url=url, final_url=url, title=f"Page {url[-20:]}", content=self.page_text,
                                         status="success", published_at="2026-09-01", retrieved_at=now))
        return pages
