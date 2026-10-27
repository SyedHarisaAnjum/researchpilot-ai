"""LLM service: invalid output, timeouts and rate limits, with a fake Gemini client."""

import asyncio

import pytest
import tenacity
from google.genai import errors, types
from pydantic import BaseModel, Field

from app.services import llm_service
from app.services.llm_service import (
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMService,
    LLMTimeoutError,
    gemini_schema,
)


class Plan(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=3)


def response(text: str, finish: str = "STOP") -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=text)]), finish_reason=finish)],
        usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=5, candidates_token_count=5),
    )


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def generate_content(self, *, model, contents, config):
        self.calls.append({"contents": contents, "max_output_tokens": config.max_output_tokens})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "hang":
            await asyncio.sleep(10)
        return outcome


@pytest.fixture
def make_service(monkeypatch):
    monkeypatch.setattr(llm_service, "wait_random_exponential", lambda **_: tenacity.wait_none())

    def make(outcomes, **settings):
        service = LLMService()
        service.settings = service.settings.model_copy(update=settings)
        models = FakeModels(outcomes)
        service.client = type("Client", (), {"aio": type("Aio", (), {"models": models})()})()
        return service, models

    return make


async def test_text_is_returned(make_service):
    service, _ = make_service([response("hello")])
    assert await service.generate_text("hi") == "hello"


async def test_structured_output_is_validated(make_service):
    service, _ = make_service([response('{"queries": ["a", "b"]}')])
    assert (await service.generate_structured("hi", Plan)).queries == ["a", "b"]


async def test_invalid_output_is_retried_with_feedback(make_service):
    service, models = make_service([response('{"queries": []}'), response('{"queries": ["ok"]}')])
    assert (await service.generate_structured("hi", Plan)).queries == ["ok"]
    assert len(models.calls) == 2
    assert "previous answer was invalid" in models.calls[1]["contents"]


async def test_invalid_output_fails_after_bounded_retries(make_service):
    service, models = make_service([response("not json")] * 3, llm_validation_retries=2)
    with pytest.raises(LLMResponseError, match="after 3 attempts"):
        await service.generate_structured("hi", Plan)
    assert len(models.calls) == 3


async def test_cut_off_output_gets_more_tokens_on_retry(make_service):
    service, models = make_service([response('{"queries": ["a"', finish="MAX_TOKENS"), response('{"queries": ["a"]}')],
                                   llm_max_output_tokens=1000)
    await service.generate_structured("hi", Plan)
    assert [c["max_output_tokens"] for c in models.calls] == [1000, 2000]


async def test_server_errors_are_retried(make_service):
    service, models = make_service([errors.ServerError(503, {"error": {"message": "busy"}}), response("ok")])
    assert await service.generate_text("hi") == "ok"
    assert len(models.calls) == 2


async def test_rate_limit_raises_after_retries(make_service):
    service, models = make_service([errors.ClientError(429, {"error": {"message": "quota"}})] * 3, llm_max_retries=3)
    with pytest.raises(LLMRateLimitError):
        await service.generate_text("hi")
    assert len(models.calls) == 3


async def test_permanent_errors_are_not_retried(make_service):
    service, models = make_service([errors.ClientError(400, {"error": {"message": "bad model"}})])
    with pytest.raises(LLMError, match="400"):
        await service.generate_text("hi")
    assert len(models.calls) == 1


async def test_timeout_raises_after_retries(make_service):
    service, models = make_service(["hang"] * 2, llm_timeout_seconds=0.05, llm_max_retries=2)
    with pytest.raises(LLMTimeoutError):
        await service.generate_text("hi")
    assert len(models.calls) == 2


async def test_blocked_prompt_is_an_error(make_service):
    blocked = types.GenerateContentResponse(prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason="SAFETY"))
    service, _ = make_service([blocked])
    with pytest.raises(LLMResponseError, match="blocked"):
        await service.generate_text("hi")


def test_schema_sent_to_gemini_has_no_length_limits():
    schema = str(gemini_schema(Plan))
    assert "minItems" not in schema and "maxItems" not in schema
    assert "queries" in schema
