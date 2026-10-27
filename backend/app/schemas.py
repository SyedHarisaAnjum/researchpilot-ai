from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

FetchStatus = Literal["success", "blocked", "unsupported", "unsafe", "failed"]


class ResearchPlanRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000, description="The question to research")


class ResearchPlan(BaseModel):
    main_question: str = Field(min_length=1, description="The user's question, restated clearly")
    subquestions: list[str] = Field(
        min_length=1, max_length=8, description="Smaller questions that together answer the main question"
    )
    search_queries: list[str] = Field(
        min_length=1, max_length=12, description="Web search queries that will find evidence for the subquestions"
    )
    research_goal: str = Field(min_length=1, description="What a complete answer must cover")


# --- LLM output schemas for the research agent (see app/agent/prompts.py) ---


class SearchQueries(BaseModel):
    queries: list[str] = Field(
        min_length=1, max_length=12, description="New web search queries, short keyword phrases"
    )


class Evidence(BaseModel):
    source_id: str = Field(description="ID of the source the evidence came from, e.g. S3")
    subquestion: str = Field(description="The subquestion this evidence helps answer, copied exactly")
    claim: str = Field(min_length=1, description="One factual statement supported by the source, in your own words")
    quote: str = Field(min_length=1, description="A short verbatim excerpt from the source that supports the claim")
    confidence: Literal["high", "medium", "low"] = Field(
        description="high: stated directly by an authoritative source; low: indirect, dated or unclear"
    )


class EvidenceExtraction(BaseModel):
    relevant: bool = Field(description="Whether the source contains anything relevant to the subquestions")
    evidence: list[Evidence] = Field(max_length=10, description="Evidence found in the source; empty if none")


class GapAnalysis(BaseModel):
    answered_subquestions: list[str] = Field(description="Subquestions the evidence answers well")
    missing_questions: list[str] = Field(
        max_length=5, description="Specific questions still unanswered or weakly supported; empty if none"
    )
    weak_evidence: list[str] = Field(
        description="Important claims resting on a single, low-confidence or non-authoritative source"
    )
    conflicts: list[str] = Field(description="Places where sources disagree, naming the source IDs")
    currency_concerns: list[str] = Field(description="Claims that may be out of date for this topic")
    is_sufficient: bool = Field(description="Whether the evidence is enough to write a reliable report")
    reasoning: str = Field(description="Brief explanation of what is covered and what is missing")


class Statement(BaseModel):
    text: str = Field(min_length=1, description="One sentence, without citation markers")
    kind: Literal["fact", "interpretation"] = Field(
        description="fact: stated by the cited sources; interpretation: your analysis or inference across sources"
    )
    source_ids: list[str] = Field(description="Source IDs that support this statement, e.g. S01; required for facts")


class DraftFinding(BaseModel):
    heading: str = Field(min_length=1)
    statements: list[Statement] = Field(min_length=1, max_length=12)


class ComparisonRow(BaseModel):
    cells: list[str] = Field(description="One value per column; write 'unknown' if the evidence does not say")
    source_ids: list[str] = Field(description="Source IDs supporting the values in this row")


class ComparisonTable(BaseModel):
    title: str = Field(description="Empty if no table is appropriate")
    columns: list[str] = Field(description="Column headings; empty if no table is appropriate")
    rows: list[ComparisonRow] = Field(description="Table rows; empty if no table is appropriate")


class ReportDraft(BaseModel):
    title: str = Field(min_length=1)
    summary: str = Field(
        min_length=1, description="Executive summary in 2-5 sentences, with inline citations like [S01]"
    )
    findings: list[DraftFinding] = Field(min_length=1, max_length=10)
    comparison: ComparisonTable
    conclusions: list[Statement] = Field(max_length=8)
    limitations: list[str] = Field(description="Gaps, conflicting evidence or caveats a reader should know")


class SupportVerdict(BaseModel):
    claim_id: str
    verdict: Literal["supported", "partially_supported", "not_supported"]
    reason: str = Field(description="One short sentence explaining the verdict")


class SupportCheck(BaseModel):
    verdicts: list[SupportVerdict]


# --- Stored records: sources and the evidence that points to them ---


class SourceRecord(BaseModel):
    source_id: str = Field(description="Stable ID such as S01, given when the page is first fetched")
    url: str
    final_url: str | None = None
    domain: str
    title: str = ""
    description: str = ""
    published_at: str | None = Field(default=None, description="Publication date found on the page, if any")
    retrieved_at: str = Field(description="When the page was fetched (UTC, ISO 8601)")
    fetch_status: FetchStatus
    error: str | None = None
    query: str = Field(description="The search query that found this source")
    round: int


class EvidenceRecord(BaseModel):
    evidence_id: str = Field(description="Stable ID such as E001")
    claim: str
    source_id: str
    source_url: str
    source_title: str
    supporting_excerpt: str = Field(description="Verbatim text from the page, checked to appear in it")
    retrieved_at: str
    subquestion: str
    confidence: Literal["high", "medium", "low"]
    round: int


# --- Final report ---

Verification = Literal["supported", "partially_supported", "not_supported", "no_citation", "unchecked"]


class VerifiedStatement(Statement):
    statement_id: str
    verification: Verification = Field(
        description="Result of checking the claim against the cited excerpts; interpretations are 'unchecked'"
    )
    verification_note: str | None = None


class ReportFinding(BaseModel):
    heading: str
    content: str = Field(description="The statements as Markdown, with citations like [S01]")
    statements: list[VerifiedStatement]
    source_ids: list[str]


class Reference(BaseModel):
    source_id: str
    title: str
    url: str
    domain: str
    published_at: str | None = Field(description="Publication date from the page; null if not found")
    retrieved_at: str = Field(description="When the agent fetched the page")


class FinalReport(BaseModel):
    title: str
    question: str
    summary: str
    methodology: list[str]
    scope: list[str]
    findings: list[ReportFinding]
    comparison: ComparisonTable | None
    conclusions: list[VerifiedStatement]
    limitations: list[str]
    unresolved_questions: list[str]
    flagged_claims: list[str] = Field(description="Claims that lack support and should not be relied on")
    verification: dict[str, int] = Field(description="Count of statements per verification result")
    sources: list[Reference] = Field(description="Built from stored source records, never written by the LLM")
    partial: bool
    insufficient_evidence: bool = Field(description="True when no verified evidence was found at all")
    stop_reason: str | None
    stats: dict[str, float | int]
    generated_at: str
    markdown: str


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    provider: str


class PageContent(BaseModel):
    url: str = Field(description="The URL that was requested")
    final_url: str | None = Field(default=None, description="The URL after redirects")
    title: str = ""
    description: str = ""
    content: str = ""
    status: FetchStatus
    error: str | None = None
    truncated: bool = False
    published_at: str | None = Field(default=None, description="Publication date found on the page, if any")
    retrieved_at: str | None = Field(default=None, description="When the page was fetched (UTC, ISO 8601)")


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=400)
    max_results: int | None = Field(default=None, ge=1, le=20)


class ReadPageRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


# --- Research job API ---


def _as_utc(value: datetime) -> datetime:
    # SQLite returns naive datetimes; every stored time is UTC.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


UTCDateTime = Annotated[datetime, AfterValidator(_as_utc)]


class ResearchRequest(BaseModel):
    question: str = Field(min_length=10, max_length=2000, description="The question to research")
    depth: Literal["quick", "standard", "deep"] = Field(
        default="standard",
        description="quick: 1 round, ~4 pages, 2 min; standard: 3 rounds, ~10 pages, 5 min; deep: 4 rounds, ~24 pages, 15 min",
    )

    @field_validator("question")
    @classmethod
    def strip_question(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 10:
            raise ValueError("Question must be at least 10 characters")
        return value


class JobSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    question: str
    depth: str
    status: str
    stage: str
    progress: int
    error: str | None
    error_code: str | None = None
    created_at: UTCDateTime
    completed_at: UTCDateTime | None


class JobDetail(JobSummary):
    cancel_requested: bool
    started_at: UTCDateTime | None
    updated_at: UTCDateTime
    limits: dict[str, Any]
    plan: dict[str, Any] | None
    stats: dict[str, Any] | None
    source_count: int = 0
    evidence_count: int = 0
    report_id: str | None = None


class JobList(BaseModel):
    jobs: list[JobSummary]
    total: int
    limit: int
    offset: int


class ReportResponse(BaseModel):
    report_id: str
    job_id: str
    completed_at: UTCDateTime
    report: dict[str, Any]


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_id: str
    url: str
    final_url: str | None
    domain: str
    title: str
    published_at: str | None
    retrieved_at: UTCDateTime
    fetch_status: str
    error: str | None
    content_length: int
    query: str
    round: int
    evidence_count: int = 0


# --- Accounts ---


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=1000)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    name: str
    is_admin: bool
    is_active: bool
    created_at: UTCDateTime
    last_login_at: UTCDateTime | None


class CreateUserRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=1000)
    name: str = Field(default="", max_length=255)
    is_admin: bool = False


class UpdateUserRequest(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=1000)
    is_admin: bool | None = None
    is_active: bool | None = None
