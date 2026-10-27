import asyncio
import logging
import time
from abc import ABC, abstractmethod
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from app.config import Settings, get_settings
from app.schemas import SearchResult

logger = logging.getLogger(__name__)

TRACKING_PARAMS = {"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "ref", "ref_src"}
MAX_SNIPPET_CHARS = 1000


class SearchError(Exception):
    """Base error for search failures that callers can turn into API errors."""


class SearchAuthError(SearchError):
    pass


class SearchRateLimitError(SearchError):
    pass


class SearchQuotaError(SearchError):
    """The provider's plan or credit limit is used up."""


class _RetryableSearchError(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code


class SearchProvider(ABC):
    """Every provider returns the same SearchResult shape."""

    name: str

    @abstractmethod
    async def search(self, query: str, max_results: int) -> list[SearchResult]: ...

    async def aclose(self) -> None:
        pass


class TavilyProvider(SearchProvider):
    name = "tavily"
    endpoint = "https://api.tavily.com/search"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            headers={"Authorization": f"Bearer {settings.tavily_api_key.get_secret_value()}"},
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        payload = {
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
        }
        retrying = AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_random_exponential(multiplier=1, max=10),
            retry=retry_if_exception_type((_RetryableSearchError, httpx.TransportError)),
            reraise=True,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    response = await self.client.post(self.endpoint, json=payload)
                    if response.status_code == 429 or response.status_code >= 500:
                        raise _RetryableSearchError(response.status_code)
        except _RetryableSearchError as exc:
            if exc.status_code == 429:
                raise SearchRateLimitError("Search provider is rate limited, try again shortly") from exc
            raise SearchError(f"Search provider error ({exc.status_code})") from exc
        except httpx.TransportError as exc:
            raise SearchError("Could not reach the search provider") from exc

        if response.status_code in (401, 403):
            raise SearchAuthError("Search provider rejected the API key")
        if response.status_code in (432, 433):
            raise SearchQuotaError("Search provider quota is used up")
        if response.status_code >= 400:
            raise SearchError(f"Search request failed ({response.status_code}): {response.text[:200]}")

        try:
            items = response.json().get("results") or []
        except ValueError as exc:
            raise SearchError("Search provider returned invalid JSON") from exc

        return [
            SearchResult(
                title=(item.get("title") or item["url"]).strip(),
                url=item["url"],
                snippet=(item.get("content") or "").strip()[:MAX_SNIPPET_CHARS],
                provider=self.name,
            )
            for item in items
            if isinstance(item, dict) and str(item.get("url", "")).startswith(("http://", "https://"))
        ]


def normalize_url(url: str) -> str:
    """A key for spotting duplicates: same page, ignoring case, www, fragments and tracking params."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/") or "/"
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
        ]
    )
    return urlunsplit(("", host, path, query, ""))


def dedupe_results(results: list[SearchResult]) -> list[SearchResult]:
    seen: set[str] = set()
    unique = []
    for result in results:
        key = normalize_url(result.url)
        if key not in seen:
            seen.add(key)
            unique.append(result)
    return unique


class SearchService:
    def __init__(self, provider: SearchProvider, default_max_results: int):
        self.provider = provider
        self.default_max_results = default_max_results

    async def aclose(self) -> None:
        await self.provider.aclose()

    async def search(self, query: str, max_results: int | None = None) -> list[SearchResult]:
        query = " ".join(query.split())
        if not query:
            return []
        limit = max_results or self.default_max_results

        start = time.perf_counter()
        results = await self.provider.search(query, limit)
        results = dedupe_results(results)[:limit]
        logger.info(
            "Search provider=%s results=%d latency=%.2fs query=%r",
            self.provider.name, len(results), time.perf_counter() - start, query,
        )
        return results

    async def search_many(self, queries: list[str], max_results_per_query: int | None = None) -> list[SearchResult]:
        """Run several queries concurrently and merge the results without duplicate URLs.

        A single failed query is logged and skipped. Auth and quota errors are
        raised, since every other query would fail the same way.
        """
        outcomes = await asyncio.gather(
            *(self.search(q, max_results_per_query) for q in queries), return_exceptions=True
        )
        merged: list[SearchResult] = []
        failures = 0
        for query, outcome in zip(queries, outcomes):
            if isinstance(outcome, (SearchAuthError, SearchQuotaError)):
                raise outcome
            if isinstance(outcome, BaseException):
                failures += 1
                logger.warning("Search failed for %r: %s", query, outcome)
                continue
            merged.extend(outcome)
        if queries and failures == len(queries):
            raise SearchError("All search queries failed")
        return dedupe_results(merged)


def build_provider(settings: Settings) -> SearchProvider:
    if settings.search_provider == "tavily":
        return TavilyProvider(settings)
    raise ValueError(f"Unknown search provider: {settings.search_provider}")


@lru_cache
def get_search_service() -> SearchService:
    settings = get_settings()
    return SearchService(build_provider(settings), settings.search_results_per_query)
