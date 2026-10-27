import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from app.config import get_settings
from app.schemas import FetchStatus, PageContent
from app.utils.url_safety import HostResolutionError, UnsafeURLError, resolve_safe_target

logger = logging.getLogger(__name__)

HTML_TYPES = {"text/html", "application/xhtml+xml"}
TEXT_TYPES = {"text/plain"}
BLOCKED_STATUS_CODES = {401, 402, 403, 407, 429, 451}
MIN_READABLE_CHARS = 200

REMOVE_TAGS = [
    "script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed",
    "form", "button", "input", "select", "textarea", "nav", "header", "footer", "aside", "dialog",
]
REMOVE_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search", "dialog", "alert"}
# Matches whole class/id words such as "cookie-banner" or "share_buttons", not "description".
NOISE_PATTERN = re.compile(
    r"(^|[-_])(cookie|consent|gdpr|newsletter|subscribe|share|social|advert|ads|promo|sponsor"
    r"|sidebar|breadcrumbs?|popup|modal|related|comments?)([-_]|$)",
    re.IGNORECASE,
)
DATE_META_NAMES = [
    "article:published_time", "og:published_time", "datePublished", "citation_publication_date",
    "dc.date", "DC.date", "dcterms.created", "pubdate", "publishdate", "date",
]
DATE_PATTERN = re.compile(r"(?<!\d)(?:19|20)\d{2}-\d{2}-\d{2}(?!\d)")
BLOCK_TAGS = [
    "p", "div", "section", "article", "main", "li", "ul", "ol", "dl", "dt", "dd", "table", "tr", "td", "th", "caption",
    "blockquote", "pre", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6",
]


class _ReadFailure(Exception):
    def __init__(self, status: FetchStatus, message: str):
        super().__init__(message)
        self.status = status


class WebReader:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.semaphore = asyncio.Semaphore(self.settings.reader_concurrency)
        self.client = httpx.AsyncClient(
            timeout=self.settings.request_timeout_seconds,
            # Redirects are followed by hand so each hop is checked for SSRF.
            follow_redirects=False,
            # Ignore proxy environment variables so requests go only where we checked.
            trust_env=False,
            headers={
                "User-Agent": self.settings.reader_user_agent,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
                "Accept-Language": "en;q=1.0, *;q=0.5",
            },
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def read(self, url: str) -> PageContent:
        """Fetch a page and extract its readable text. Never raises; failures are in `status`."""
        start = time.perf_counter()
        async with self.semaphore:
            retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            page = await self._read(url)
        page.retrieved_at = retrieved_at
        log = logger.info if page.status == "success" else logger.warning
        log(
            "Read status=%s chars=%d latency=%.2fs url=%s%s",
            page.status, len(page.content), time.perf_counter() - start, url,
            f" error={page.error}" if page.error else "",
        )
        return page

    async def read_many(self, urls: list[str]) -> list[PageContent]:
        return list(await asyncio.gather(*(self.read(url) for url in urls)))

    async def _read(self, url: str) -> PageContent:
        try:
            final_url, content_type, body, charset = await self._fetch(url)
        except UnsafeURLError as exc:
            return PageContent(url=url, status="unsafe", error=str(exc))
        except HostResolutionError as exc:
            return PageContent(url=url, status="failed", error=str(exc))
        except _ReadFailure as exc:
            return PageContent(url=url, status=exc.status, error=str(exc))
        except httpx.TimeoutException:
            return PageContent(url=url, status="failed", error="Timed out")
        except httpx.HTTPError as exc:
            return PageContent(url=url, status="failed", error=f"Network error: {type(exc).__name__}")

        if content_type in TEXT_TYPES:
            title, description, published_at = "", "", None
            text = _clean_text(body.decode(charset or "utf-8", errors="replace"))
        else:
            title, description, published_at, text = _parse_html(body, charset)

        content, truncated = _truncate(text, self.settings.reader_max_content_chars)
        page = PageContent(
            url=url, final_url=final_url, title=title, description=description,
            content=content, status="success", truncated=truncated, published_at=published_at,
        )
        if len(content) < MIN_READABLE_CHARS:
            page.status = "failed"
            page.error = "Page has too little readable text; it may be empty or require JavaScript"
        return page

    async def _fetch(self, url: str) -> tuple[str, str, bytes, str | None]:
        current = url
        for _ in range(self.settings.reader_max_redirects + 1):
            target = await resolve_safe_target(current)
            request = self.client.build_request(
                "GET",
                target.pinned_url,
                headers={"Host": target.host_header},
                # Keeps TLS certificate checks against the real hostname, not the IP.
                extensions={"sni_hostname": target.host} if target.scheme == "https" else {},
            )
            response = await self.client.send(request, stream=True)
            try:
                if response.is_redirect:
                    current = urljoin(current, response.headers["location"])
                    continue
                if response.status_code in BLOCKED_STATUS_CODES:
                    raise _ReadFailure("blocked", f"Site refused access (HTTP {response.status_code})")
                if response.status_code >= 400:
                    raise _ReadFailure("failed", f"HTTP {response.status_code}")

                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if content_type and content_type not in HTML_TYPES | TEXT_TYPES:
                    raise _ReadFailure("unsupported", f"Unsupported content type: {content_type}")

                body = await self._read_body(response)
                return target.url, content_type or "text/html", body, response.charset_encoding
            finally:
                await response.aclose()
        raise _ReadFailure("failed", "Too many redirects")

    async def _read_body(self, response: httpx.Response) -> bytes:
        """Download at most reader_max_download_bytes; a partial HTML page still parses."""
        limit = self.settings.reader_max_download_bytes
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) >= limit:
                break
        return bytes(body[:limit])


def _parse_html(body: bytes, charset: str | None) -> tuple[str, str, str | None, str]:
    """Returns (title, description, published_at, text)."""
    soup = BeautifulSoup(body, "html.parser", from_encoding=charset)
    title = _meta(soup, "og:title") or (soup.title.get_text(strip=True) if soup.title else "")
    description = _meta(soup, "description") or _meta(soup, "og:description")
    # Read before scripts are removed: the date is often in JSON-LD.
    published_at = _published_date(soup)

    for tag in soup(REMOVE_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        if tag.decomposed or tag.attrs is None:
            continue
        noise_words = [*tag.get("class", []), tag.get("id") or ""]
        if (
            tag.get("role") in REMOVE_ROLES
            or tag.get("aria-hidden") == "true"
            or tag.has_attr("hidden")
            or any(NOISE_PATTERN.search(word) for word in noise_words)
        ) and tag.name not in ("html", "body", "main", "article"):
            tag.decompose()

    root = _main_content(soup)
    for br in root.find_all("br"):
        br.replace_with("\n")
    for tag in root.find_all(BLOCK_TAGS):
        tag.insert_after("\n")
    return " ".join(title.split()), " ".join(description.split()), published_at, _clean_text(root.get_text())


def _published_date(soup: BeautifulSoup) -> str | None:
    """Publication date from common metadata, as YYYY-MM-DD. None if the page doesn't state one."""
    candidates = [_meta(soup, name) for name in DATE_META_NAMES]
    tag = soup.find(attrs={"itemprop": "datePublished"})
    if isinstance(tag, Tag):
        candidates.append(str(tag.get("content") or tag.get("datetime") or ""))
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if match := re.search(r'"datePublished"\s*:\s*"([^"]+)"', script.get_text()):
            candidates.append(match.group(1))
    time_tag = soup.find("time", attrs={"datetime": True})
    if isinstance(time_tag, Tag):
        candidates.append(str(time_tag["datetime"]))
    for value in candidates:
        if match := DATE_PATTERN.search(value or ""):
            return match.group(0)
    return None


def _meta(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
    content = tag.get("content") if isinstance(tag, Tag) else None
    return content.strip() if isinstance(content, str) else ""


def _main_content(soup: BeautifulSoup) -> Tag:
    """Use <main> or the largest <article> when it holds real content, else the whole body."""
    candidates = soup.find_all("main") + soup.find_all("article")
    best = max(candidates, key=lambda t: len(t.get_text(strip=True)), default=None)
    if best is not None and len(best.get_text(strip=True)) >= MIN_READABLE_CHARS:
        return best
    return soup.body or soup


def _clean_text(text: str) -> str:
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    cut = text.rfind(" ", 0, limit)
    return text[: cut if cut > limit * 0.8 else limit].rstrip(), True


@lru_cache
def get_web_reader() -> WebReader:
    return WebReader()
