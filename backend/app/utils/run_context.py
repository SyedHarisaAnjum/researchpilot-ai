"""Per-job context: the current job ID (for logs) and its token usage (for the token budget).

Context variables follow asyncio tasks, so every LLM call made while a job runs, including
ones started with asyncio.gather, is counted against that job.
"""

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass


@dataclass
class Usage:
    llm_calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens + self.thinking_tokens

    def as_dict(self) -> dict[str, int]:
        return {**asdict(self), "total_tokens": self.total_tokens}


current_job_id: ContextVar[str | None] = ContextVar("current_job_id", default=None)
current_usage: ContextVar[Usage | None] = ContextVar("current_usage", default=None)


@contextmanager
def research_context(job_id: str) -> Iterator[Usage]:
    usage = Usage()
    job_token, usage_token = current_job_id.set(job_id), current_usage.set(usage)
    try:
        yield usage
    finally:
        current_job_id.reset(job_token)
        current_usage.reset(usage_token)


def record_usage(prompt: int | None, output: int | None, thinking: int | None) -> None:
    if usage := current_usage.get():
        usage.llm_calls += 1
        usage.prompt_tokens += prompt or 0
        usage.output_tokens += output or 0
        usage.thinking_tokens += thinking or 0


def tokens_used() -> int | None:
    usage = current_usage.get()
    return usage.total_tokens if usage else None


class JobContextFilter(logging.Filter):
    """Adds `job` (the running job's ID, or "-") to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.job = (current_job_id.get() or "-")[:8]
        return True


class SecretRedactionFilter(logging.Filter):
    """Replaces any configured secret that reaches a log message with ***."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        values = sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True)
        self.pattern = re.compile("|".join(map(re.escape, values))) if values else None

    def filter(self, record: logging.LogRecord) -> bool:
        if self.pattern:
            message = record.getMessage()
            redacted = self.pattern.sub("***", message)
            if redacted != message:
                record.msg, record.args = redacted, None
        return True
