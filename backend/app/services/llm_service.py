import asyncio
import logging
import time
from functools import lru_cache
from typing import Any, TypeVar

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from app.config import get_settings
from app.utils.run_context import record_usage

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
MAX_OUTPUT_TOKENS_CAP = 32768

# Length and range limits make Gemini's constrained decoding too complex for nested schemas
# ("schema produces a constraint that has too many states"), so they are left out of what
# Gemini is sent and checked by Pydantic afterwards instead.
_SCHEMA_LIMIT_KEYS = {"minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum",
                      "exclusiveMinimum", "exclusiveMaximum", "pattern"}


def _strip_limits(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_limits(v) for k, v in node.items() if k not in _SCHEMA_LIMIT_KEYS}
    if isinstance(node, list):
        return [_strip_limits(v) for v in node]
    return node


@lru_cache
def gemini_schema(schema: type[BaseModel]) -> dict[str, Any]:
    return _strip_limits(schema.model_json_schema())


def _finish_reason(response: types.GenerateContentResponse) -> types.FinishReason | None:
    return response.candidates[0].finish_reason if response.candidates else None


class LLMError(Exception):
    """Base error for LLM failures that callers can turn into API errors."""


class LLMTimeoutError(LLMError):
    pass


class LLMRateLimitError(LLMError):
    pass


class LLMResponseError(LLMError):
    """The model answered, but the answer was empty, blocked or invalid."""


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, errors.APIError):
        return exc.code in RETRYABLE_STATUS_CODES
    return isinstance(exc, (asyncio.TimeoutError, httpx.TransportError))


class LLMService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.model = self.settings.gemini_model
        self.client = genai.Client(
            api_key=self.settings.gemini_api_key.get_secret_value(),
            http_options=types.HttpOptions(timeout=int(self.settings.llm_timeout_seconds * 1000)),
        )

    async def aclose(self) -> None:
        await self.client.aio.aclose()

    async def generate_text(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        config = self._build_config(system_instruction, temperature, max_output_tokens)
        response = await self._generate(prompt, config, purpose="text")
        return self._extract_text(response)

    async def generate_structured(
        self,
        prompt: str,
        schema: type[T],
        *,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> T:
        config = self._build_config(system_instruction, temperature, max_output_tokens)
        config.response_mime_type = "application/json"
        config.response_json_schema = gemini_schema(schema)

        # Invalid output is retried a few times, telling the model what was wrong.
        attempts = self.settings.llm_validation_retries + 1
        contents = prompt
        for attempt in range(1, attempts + 1):
            response = await self._generate(contents, config, purpose=schema.__name__)
            try:
                # Pydantic enforces the full schema, including the limits Gemini wasn't sent.
                return schema.model_validate_json(self._extract_text(response))
            except (ValidationError, LLMResponseError) as exc:
                logger.warning(
                    "LLM output failed %s validation (attempt %d/%d): %s",
                    schema.__name__, attempt, attempts, exc,
                )
                if _finish_reason(response) == types.FinishReason.MAX_TOKENS:
                    # Cut off mid-JSON: give the next attempt more room.
                    config.max_output_tokens = min(config.max_output_tokens * 2, MAX_OUTPUT_TOKENS_CAP)
                contents = (
                    f"{prompt}\n\nYour previous answer was invalid: {exc}\n"
                    f"Return only JSON that matches the required schema."
                )
        raise LLMResponseError(f"Model output did not match {schema.__name__} after {attempts} attempts")

    def _build_config(
        self,
        system_instruction: str | None,
        temperature: float | None,
        max_output_tokens: int | None,
    ) -> types.GenerateContentConfig:
        budget = self.settings.llm_thinking_budget
        return types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=self.settings.llm_temperature if temperature is None else temperature,
            max_output_tokens=max_output_tokens or self.settings.llm_max_output_tokens,
            thinking_config=types.ThinkingConfig(thinking_budget=budget) if budget is not None else None,
        )

    async def _generate(
        self, prompt: str, config: types.GenerateContentConfig, purpose: str
    ) -> types.GenerateContentResponse:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self.settings.llm_max_retries),
            wait=wait_random_exponential(multiplier=1, max=20),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
            before_sleep=lambda state: logger.warning(
                "LLM call failed (attempt %d), retrying: %r",
                state.attempt_number,
                state.outcome.exception(),
            ),
        )
        start = time.perf_counter()
        try:
            async for attempt in retrying:
                with attempt:
                    response = await asyncio.wait_for(
                        self.client.aio.models.generate_content(
                            model=self.model, contents=prompt, config=config
                        ),
                        timeout=self.settings.llm_timeout_seconds,
                    )
        except asyncio.TimeoutError as exc:
            raise LLMTimeoutError("The model did not respond in time") from exc
        except errors.APIError as exc:
            if exc.code == 429:
                raise LLMRateLimitError("The model is rate limited, try again shortly") from exc
            raise LLMError(f"Model request failed ({exc.code}): {exc.message}") from exc
        except httpx.TransportError as exc:
            raise LLMError("Could not reach the model API") from exc

        self._log_usage(response, purpose, time.perf_counter() - start)
        return response

    def _extract_text(self, response: types.GenerateContentResponse) -> str:
        text = response.text
        if text:
            return text
        feedback = response.prompt_feedback
        if feedback and feedback.block_reason:
            raise LLMResponseError(f"Prompt was blocked: {feedback.block_reason}")
        finish = response.candidates[0].finish_reason if response.candidates else None
        raise LLMResponseError(f"Model returned no text (finish reason: {finish})")

    def _log_usage(self, response: types.GenerateContentResponse, purpose: str, seconds: float) -> None:
        usage = response.usage_metadata
        finish = response.candidates[0].finish_reason if response.candidates else None
        if usage:
            record_usage(usage.prompt_token_count, usage.candidates_token_count, usage.thoughts_token_count)
        logger.info(
            "LLM %s model=%s latency=%.2fs prompt_tokens=%s output_tokens=%s thinking_tokens=%s total_tokens=%s finish=%s",
            purpose,
            self.model,
            seconds,
            usage.prompt_token_count if usage else None,
            usage.candidates_token_count if usage else None,
            usage.thoughts_token_count if usage else None,
            usage.total_token_count if usage else None,
            finish,
        )
        if finish == types.FinishReason.MAX_TOKENS:
            logger.warning("LLM %s output was cut off at max_output_tokens", purpose)


@lru_cache
def get_llm_service() -> LLMService:
    return LLMService()
