"""Search: empty results, duplicate URLs and provider failures, against a fake Tavily."""

import json

import httpx
import pytest
import tenacity

from app.services import search_service
from app.services.search_service import (
    SearchAuthError,
    SearchError,
    SearchQuotaError,
    SearchRateLimitError,
    SearchService,
    TavilyProvider,
    normalize_url,
)
from app.config import get_settings


@pytest.fixture
def tavily(monkeypatch):
    """A SearchService whose Tavily provider talks to a mock HTTP handler."""
    monkeypatch.setattr(search_service, "wait_random_exponential", lambda **_: tenacity.wait_none())

    def make(handler):
        provider = TavilyProvider(get_settings())
        calls = []

        def record(request):
            calls.append(json.loads(request.content))
            return handler(request, len(calls))

        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(record), headers=provider.client.headers)
        return SearchService(provider, default_max_results=5), calls

    return make


def results(*urls):
    return {"results": [{"title": f"T{i}", "url": u, "content": f"snippet {i}"} for i, u in enumerate(urls)]}


async def test_results_are_normalized(tavily):
    service, calls = tavily(lambda r, n: httpx.Response(200, json=results("https://a.com/x")))
    [result] = await service.search("  free   llm  ")
    assert result.model_dump() == {"title": "T0", "url": "https://a.com/x", "snippet": "snippet 0", "provider": "tavily"}
    assert calls[0]["query"] == "free llm"
    assert calls[0]["max_results"] == 5


async def test_empty_results(tavily):
    service, _ = tavily(lambda r, n: httpx.Response(200, json={"results": []}))
    assert await service.search("nothing") == []


async def test_blank_query_makes_no_request(tavily):
    service, calls = tavily(lambda r, n: httpx.Response(200, json=results()))
    assert await service.search("   ") == []
    assert calls == []


async def test_duplicate_urls_are_removed(tavily):
    duplicates = results(
        "https://example.com/docs/", "https://www.EXAMPLE.com/docs?utm_source=x#top",
        "https://example.com/docs?fbclid=1", "https://other.org/page",
    )
    service, _ = tavily(lambda r, n: httpx.Response(200, json=duplicates))
    assert [r.url for r in await service.search("q")] == ["https://example.com/docs/", "https://other.org/page"]


async def test_result_count_is_limited_and_non_http_urls_dropped(tavily):
    many = results(*[f"https://site{i}.com" for i in range(8)], "javascript:alert(1)", "ftp://x.com")
    service, _ = tavily(lambda r, n: httpx.Response(200, json=many))
    found = await service.search("q", max_results=3)
    assert len(found) == 3 and all(r.url.startswith("https://") for r in found)


@pytest.mark.parametrize(
    "status, error, attempts",
    [(401, SearchAuthError, 1), (432, SearchQuotaError, 1), (429, SearchRateLimitError, 3), (500, SearchError, 3), (400, SearchError, 1)],
)
async def test_provider_failures(tavily, status, error, attempts):
    service, calls = tavily(lambda r, n: httpx.Response(status, json={"detail": "x"}))
    with pytest.raises(error):
        await service.search("q")
    assert len(calls) == attempts


async def test_temporary_failure_then_success(tavily):
    service, calls = tavily(lambda r, n: httpx.Response(503) if n == 1 else httpx.Response(200, json=results("https://a.com")))
    assert len(await service.search("q")) == 1
    assert len(calls) == 2


async def test_network_failure(tavily):
    def fail(request, n):
        raise httpx.ConnectError("down")

    service, _ = tavily(fail)
    with pytest.raises(SearchError, match="reach"):
        await service.search("q")


async def test_search_many_skips_failed_queries_and_dedupes(tavily):
    def handler(request, n):
        query = json.loads(request.content)["query"]
        return httpx.Response(500) if query == "bad" else httpx.Response(200, json=results("https://shared.com", f"https://{query}.com"))

    service, _ = tavily(handler)
    found = await service.search_many(["good", "bad", "fine"])
    assert [r.url for r in found] == ["https://shared.com", "https://good.com", "https://fine.com"]


async def test_search_many_raises_when_quota_is_gone(tavily):
    service, _ = tavily(lambda r, n: httpx.Response(432))
    with pytest.raises(SearchQuotaError):
        await service.search_many(["a", "b"])


def test_normalize_url():
    assert normalize_url("https://WWW.Example.com/Path/?utm_medium=x&id=3#frag") == "//example.com/Path?id=3"
