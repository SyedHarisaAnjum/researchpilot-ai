"""Web reader and URL safety: invalid URLs, unsupported content, redirects and SSRF."""

import httpx
import pytest

from app.services import web_reader
from app.services.web_reader import WebReader, _parse_html
from app.utils.url_safety import SafeTarget, UnsafeURLError, is_public_ip, resolve_safe_target, validate_url
import ipaddress

ARTICLE = (
    "<html><head><title>Docs page</title><meta name='description' content='About limits'>"
    "<meta property='article:published_time' content='2026-05-02T10:00:00Z'></head><body>"
    "<nav>Home | Pricing | Login</nav><script>var tracking = 1;</script>"
    "<div class='cookie-banner'>We use cookies</div>"
    "<article><h1>Rate limits</h1><p>" + "The free tier allows 15 requests per minute. " * 10 + "</p></article>"
    "<footer>Copyright</footer></body></html>"
)

# Hostname -> IP for the fake DNS. Private addresses must be refused.
HOSTS = {"docs.example": "93.184.216.34", "other.example": "93.184.216.35", "internal.example": "10.0.0.5"}


async def fake_resolve(url):
    scheme, host, port = validate_url(url)
    ip = HOSTS.get(host) or host
    if not is_public_ip(ipaddress.ip_address(ip)):
        raise UnsafeURLError(f"Host '{host}' resolves to a non-public address")
    return SafeTarget(url=url, scheme=scheme, host=host, port=port, ip=ip)


def site(request: httpx.Request) -> httpx.Response:
    host, path = request.headers["host"], request.url.path
    if host == "other.example":
        return httpx.Response(200, html=ARTICLE)
    routes = {
        "/article": lambda: httpx.Response(200, html=ARTICLE),
        "/pdf": lambda: httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}),
        "/text": lambda: httpx.Response(200, text="Plain text facts. " * 30, headers={"content-type": "text/plain"}),
        "/redirect": lambda: httpx.Response(302, headers={"location": "https://other.example/final"}),
        "/redirect-relative": lambda: httpx.Response(301, headers={"location": "/article"}),
        "/redirect-private": lambda: httpx.Response(302, headers={"location": "http://internal.example/admin"}),
        "/redirect-metadata": lambda: httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"}),
        "/loop": lambda: httpx.Response(302, headers={"location": "/loop"}),
        "/forbidden": lambda: httpx.Response(403),
        "/missing": lambda: httpx.Response(404),
        "/js-only": lambda: httpx.Response(200, html="<html><body><div id='root'></div><script>app()</script></body></html>"),
        "/huge": lambda: httpx.Response(200, html="<html><body><p>" + "word " * 20000 + "</p></body></html>"),
    }
    return routes.get(path, lambda: httpx.Response(404))()


@pytest.fixture
def reader(monkeypatch):
    monkeypatch.setattr(web_reader, "resolve_safe_target", fake_resolve)
    instance = WebReader()
    instance.client = httpx.AsyncClient(transport=httpx.MockTransport(site), follow_redirects=False)
    return instance


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd", "ftp://docs.example/", "javascript:alert(1)", "not a url", "http://user:pw@docs.example/",
        "http://docs.example:22/", "http://localhost/", "http://printer.local/", "http://metadata.google.internal/",
        "http://127.0.0.1/", "http://10.0.0.1/", "http://192.168.1.1/", "http://169.254.169.254/", "http://[::1]/",
        "http://[::ffff:127.0.0.1]/", "http://" + "a" * 2100 + ".com/",
    ],
)
async def test_unsafe_urls_are_refused(url):
    with pytest.raises(UnsafeURLError):
        await resolve_safe_target(url)


async def test_unsafe_url_is_reported_not_fetched(reader):
    page = await reader.read("http://internal.example/admin")
    assert page.status == "unsafe" and page.content == ""


async def test_article_text_is_extracted(reader):
    page = await reader.read("https://docs.example/article")
    assert page.status == "success"
    assert page.title == "Docs page" and page.description == "About limits"
    assert page.published_at == "2026-05-02"
    assert page.retrieved_at.endswith("Z")
    assert "15 requests per minute" in page.content
    for noise in ("Home | Pricing", "tracking", "cookies", "Copyright"):
        assert noise not in page.content


async def test_plain_text_is_supported(reader):
    page = await reader.read("https://docs.example/text")
    assert page.status == "success" and "Plain text facts" in page.content


@pytest.mark.parametrize(
    "path, status, error",
    [
        ("/pdf", "unsupported", "application/pdf"),
        ("/forbidden", "blocked", "403"),
        ("/missing", "failed", "404"),
        ("/js-only", "failed", "JavaScript"),
        ("/loop", "failed", "Too many redirects"),
    ],
)
async def test_failures_are_recorded_not_hidden(reader, path, status, error):
    page = await reader.read(f"https://docs.example{path}")
    assert page.status == status
    assert error in page.error


async def test_redirects_are_followed(reader):
    page = await reader.read("https://docs.example/redirect")
    assert page.status == "success" and page.final_url == "https://other.example/final"
    relative = await reader.read("https://docs.example/redirect-relative")
    assert relative.final_url == "https://docs.example/article"


@pytest.mark.parametrize("path", ["/redirect-private", "/redirect-metadata"])
async def test_redirects_to_private_addresses_are_refused(reader, path):
    page = await reader.read(f"https://docs.example{path}")
    assert page.status == "unsafe"


async def test_long_pages_are_truncated(reader):
    page = await reader.read("https://docs.example/huge")
    assert page.truncated and len(page.content) <= reader.settings.reader_max_content_chars


async def test_requests_go_to_the_checked_ip():
    seen = []

    def capture(request):
        seen.append((request.url.host, request.headers["host"]))
        return httpx.Response(200, html=ARTICLE)

    reader = WebReader()
    reader.client = httpx.AsyncClient(transport=httpx.MockTransport(capture))
    import app.services.web_reader as module

    original = module.resolve_safe_target
    module.resolve_safe_target = fake_resolve
    try:
        await reader.read("https://docs.example/article")
    finally:
        module.resolve_safe_target = original
    # Connects to the IP that passed the check (no second DNS lookup), with the real Host header.
    assert seen == [("93.184.216.34", "docs.example")]


def test_published_date_sources():
    jsonld = b'<html><head><script type="application/ld+json">{"datePublished": "2024-06-12"}</script></head><body>x</body></html>'
    time_tag = b'<html><body><time datetime="2023-01-09">Jan 9</time></body></html>'
    none = b"<html><body>no date</body></html>"
    assert [_parse_html(h, None)[2] for h in (jsonld, time_tag, none)] == ["2024-06-12", "2023-01-09", None]
