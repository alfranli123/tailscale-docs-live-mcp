#!/usr/bin/env python3
"""
MCP server that provides live access to Tailscale documentation
(tailscale.com/docs) via the public search API, page HTML extraction,
and sitemap inventory.

Tools: search_docs, get_doc, get_doc_outline, list_docs
"""

import asyncio
import atexit
import re
import time
import html as html_mod
from collections import OrderedDict
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from mcp.server.fastmcp import FastMCP

# ── Configuration ────────────────────────────────────────────────────────────

SEARCH_URL = "https://tailscale.com/api/search"
SITEMAP_URL = "https://tailscale.com/sitemap.xml"
DOCS_BASE = "https://tailscale.com"
USER_AGENT = "tailscale-docs-live-mcp/0.3 (+https://github.com/alfranli123/tailscale-docs-live-mcp)"
REQUEST_TIMEOUT = 20  # seconds
CACHE_TTL = 600  # seconds (10 min)
CACHE_MAX_ENTRIES = 200
HTTP_MAX_RETRIES = 3
HTTP_RETRY_BACKOFF = 1.0  # seconds; doubled after each retry
HTTP_RETRY_STATUS_CODES = frozenset({429, 502, 503, 504})
MAX_CONTENT_CHARS = 12000

# ── Server instance ──────────────────────────────────────────────────────────

mcp = FastMCP(
    "tailscale-docs-live",
    instructions=(
        "Live Tailscale documentation via public search API and page extraction. "
        "Suggested workflow: search_docs to find pages, get_doc_outline to skim "
        "structure, get_doc (with offset when truncated) to read content, and "
        "list_docs to browse the sitemap by prefix."
    ),
)

# ── Bounded TTL + LRU cache ──────────────────────────────────────────────────


class _TTLCache:
    """In-memory cache with TTL expiry and LRU eviction."""

    def __init__(self, max_entries: int, ttl_seconds: float) -> None:
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._entries: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        ts, val = entry
        if time.monotonic() - ts > self._ttl_seconds:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return val

    def set(self, key: str, val: Any) -> None:
        if key in self._entries:
            del self._entries[key]
        self._entries[key] = (time.monotonic(), val)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()


_cache = _TTLCache(CACHE_MAX_ENTRIES, CACHE_TTL)


def _cached(key: str) -> Any | None:
    return _cache.get(key)


def _set_cache(key: str, val: Any) -> None:
    _cache.set(key, val)


async def _reset_runtime_state() -> None:
    """Clear cache and close the HTTP client (for tests)."""
    _cache.clear()
    await _close_client()


# ── Shared HTTP client (lazy‑initialised singleton, follow_redirects=True) ───

_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
    return _client


def _retry_delay_seconds(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return max(float(retry_after), 0.0)
            except ValueError:
                pass
    return HTTP_RETRY_BACKOFF * (2**attempt)


async def _close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _close_client_sync() -> None:
    global _client
    if _client is None:
        return
    client = _client
    _client = None
    try:
        asyncio.run(client.aclose())
    except RuntimeError:
        pass


atexit.register(_close_client_sync)


async def _http_get(
    url: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    """GET a URL, return parsed JSON or an error dict."""
    client = await _get_client()
    last_error: dict[str, Any] | None = None

    for attempt in range(HTTP_MAX_RETRIES + 1):
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "json" in content_type:
                return resp.json()
            return {"_html": resp.text, "_status": resp.status_code}
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            last_error = {
                "error": f"HTTP {status} from {url}",
                "status": status,
            }
            if status in HTTP_RETRY_STATUS_CODES and attempt < HTTP_MAX_RETRIES:
                await asyncio.sleep(_retry_delay_seconds(exc.response, attempt))
                continue
            return last_error
        except httpx.RequestError as exc:
            last_error = {"error": f"Request failed: {exc}"}
            if attempt < HTTP_MAX_RETRIES:
                await asyncio.sleep(_retry_delay_seconds(None, attempt))
                continue
            return last_error
        except Exception as exc:
            return {"error": str(exc)}

    return last_error or {"error": f"Request failed for {url}"}


# ── HTML → plain‑text converter (stdlib only) ────────────────────────────────


class _HTMLToText(HTMLParser):
    """Convert HTML to readable plain‑text / markdown‑ish output."""

    def __init__(self) -> None:
        super().__init__()
        self._output: list[str] = []
        self._skip = 0  # nesting depth of <script>/<style>
        self._pre_depth = 0
        self._links: list[dict[str, Any]] = []
        self._tables: list[dict[str, Any]] = []

    def _emit(self, text: str) -> None:
        if self._skip > 0:
            return
        self._output.append(text)

    def _maybe_newline(self) -> None:
        if self._output and not self._output[-1].endswith("\n"):
            self._emit("\n")

    def _current_table(self) -> dict[str, Any] | None:
        return self._tables[-1] if self._tables else None

    def _start_table_row(self, table: dict[str, Any]) -> None:
        if table["row_active"]:
            self._end_table_row(table)
        self._maybe_newline()
        self._emit("| ")
        table["row_active"] = True
        table["cell_count"] = 0
        table["row_is_header"] = table["thead_depth"] > 0 or table["rows_seen"] == 0
        table["in_cell"] = False
        table["cell_tag"] = None

    def _end_table_row(self, table: dict[str, Any]) -> None:
        if not table["row_active"]:
            return
        cell_count = max(1, table["cell_count"])
        self._emit(" |\n")
        if table["row_is_header"] and not table["header_separator_emitted"]:
            self._emit("|" + "---|" * cell_count + "\n")
            table["header_separator_emitted"] = True
        table["rows_seen"] += 1
        table["row_active"] = False
        table["cell_count"] = 0
        table["in_cell"] = False
        table["cell_tag"] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_low = tag.lower()
        if tag_low in ("script", "style"):
            self._skip += 1
            return
        if self._skip > 0:
            return
        if tag_low in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._maybe_newline()
            prefix = "#" * int(tag_low[1]) + " "
            self._emit(f"\n{prefix}")
        elif tag_low == "p":
            self._maybe_newline()
            self._emit("\n")
        elif tag_low == "br":
            self._emit("\n")
        elif tag_low == "hr":
            self._maybe_newline()
            self._emit("\n---\n")
        elif tag_low == "li":
            self._maybe_newline()
            self._emit("  - ")
        elif tag_low == "a":
            href = next((value for name, value in attrs if name.lower() == "href"), None)
            href = href.strip() if href else ""
            if not href or href.startswith("#") or href.lower().startswith("javascript:"):
                href = ""
            elif not urlparse(href).scheme and not href.startswith("//"):
                href = urljoin(f"{DOCS_BASE}/", href)
            elif href.startswith("//"):
                href = urljoin(f"{DOCS_BASE}/", href)
            if self._links:
                self._links[-1]["nested"] = True
            self._links.append({"href": href, "start": len(self._output), "nested": False})
        elif tag_low == "table":
            self._maybe_newline()
            self._tables.append(
                {
                    "rows_seen": 0,
                    "row_active": False,
                    "cell_count": 0,
                    "row_is_header": False,
                    "header_separator_emitted": False,
                    "thead_depth": 0,
                    "in_cell": False,
                    "cell_tag": None,
                }
            )
        elif tag_low == "thead":
            table = self._current_table()
            if table is not None:
                table["thead_depth"] += 1
        elif tag_low == "tr":
            table = self._current_table()
            if table is not None:
                self._start_table_row(table)
            else:
                self._maybe_newline()
        elif tag_low in ("th", "td"):
            table = self._current_table()
            if table is not None:
                if not table["row_active"]:
                    self._start_table_row(table)
                if table["cell_count"] > 0:
                    self._emit(" | ")
                table["cell_count"] += 1
                table["in_cell"] = True
                table["cell_tag"] = tag_low
        elif tag_low in ("ol", "ul"):
            self._maybe_newline()
        elif tag_low == "blockquote":
            self._maybe_newline()
            self._emit("\n> ")
        elif tag_low == "code":
            if self._pre_depth == 0:
                self._emit("`")
        elif tag_low == "pre":
            self._pre_depth += 1
            self._maybe_newline()
            self._emit("\n```\n")

    def handle_endtag(self, tag: str) -> None:
        tag_low = tag.lower()
        if tag_low in ("script", "style"):
            self._skip = max(0, self._skip - 1)
            return
        if self._skip > 0:
            return
        if tag_low == "a":
            if not self._links:
                return
            link = self._links.pop()
            href = link["href"]
            if not href or link["nested"]:
                return
            text = "".join(self._output[link["start"]:])
            if not text.strip():
                return
            self._output[link["start"]:] = [f"[{text}]({href})"]
            return
        if tag_low == "li":
            self._emit("\n")
        elif tag_low == "p":
            self._emit("\n")
        elif tag_low in ("th", "td"):
            table = self._current_table()
            if table is not None and table["cell_tag"] == tag_low:
                table["in_cell"] = False
                table["cell_tag"] = None
        elif tag_low == "tr":
            table = self._current_table()
            if table is not None:
                self._end_table_row(table)
            else:
                self._emit("\n")
        elif tag_low == "thead":
            table = self._current_table()
            if table is not None:
                table["thead_depth"] = max(0, table["thead_depth"] - 1)
        elif tag_low == "table":
            table = self._current_table()
            if table is not None:
                self._end_table_row(table)
                self._tables.pop()
                self._maybe_newline()
        elif tag_low == "blockquote":
            self._emit("\n")
        elif tag_low == "pre":
            if self._pre_depth > 0:
                self._emit("\n```\n")
                self._pre_depth -= 1
        elif tag_low == "code":
            if self._pre_depth == 0:
                self._emit("`")

    def handle_data(self, data: str) -> None:
        if self._skip > 0:
            return
        if self._pre_depth > 0:
            text = data
        else:
            text = re.sub(r"\s+", " ", data).strip()
        if text:
            table = self._current_table()
            if table is not None and table["row_active"] and table["in_cell"]:
                text = text.replace("|", r"\|")
            self._emit(text)

    def handle_entityref(self, name: str) -> None:
        if self._skip > 0:
            return
        char = html_mod.unescape(f"&{name};")
        self._emit(char)

    def handle_charref(self, name: str) -> None:
        if self._skip > 0:
            return
        try:
            if name.startswith("x") or name.startswith("X"):
                code = int(name[1:], 16) if name[1:] else ord("?")
            else:
                code = int(name)
        except ValueError:
            code = ord("?")
        try:
            self._emit(chr(code))
        except (ValueError, OverflowError):
            self._emit("?")

    def get_text(self) -> str:
        raw = "".join(self._output)
        # Collapse multiple blank lines
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def html_to_text(html: str) -> str:
    """Return a readable plain‑text version of an HTML string."""
    parser = _HTMLToText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    return parser.get_text()


class _HeadingExtractor(HTMLParser):
    """Extract heading structure (level, text, id) from HTML."""

    def __init__(self, max_level: int = 3) -> None:
        super().__init__()
        self._max_level = max_level
        self._skip = 0
        self._in_heading: int | None = None
        self._heading_id = ""
        self._heading_parts: list[str] = []
        self.headings: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_low = tag.lower()
        if tag_low in ("script", "style"):
            self._skip += 1
            return
        if self._skip > 0:
            return
        if tag_low in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag_low[1])
            if level <= self._max_level:
                self._in_heading = level
                self._heading_id = next(
                    (value for name, value in attrs if name.lower() == "id"),
                    "",
                ) or ""
                self._heading_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag_low = tag.lower()
        if tag_low in ("script", "style"):
            self._skip = max(0, self._skip - 1)
            return
        if self._skip > 0:
            return
        if self._in_heading is not None and tag_low == f"h{self._in_heading}":
            text = html_mod.unescape("".join(self._heading_parts))
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                anchor_id = self._heading_id or _slugify_heading(text)
                self.headings.append(
                    {
                        "level": self._in_heading,
                        "text": text,
                        "id": anchor_id,
                        "anchor": f"#{anchor_id}",
                    }
                )
            self._in_heading = None
            self._heading_id = ""
            self._heading_parts = []

    def handle_data(self, data: str) -> None:
        if self._skip > 0 or self._in_heading is None:
            return
        self._heading_parts.append(data)


def _slugify_heading(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "section"


def extract_outline(html: str, max_level: int = 3) -> list[dict[str, Any]]:
    """Return heading outline from HTML."""
    parser = _HeadingExtractor(max_level=max_level)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    return parser.headings


def _extract_body_html(html: str) -> str:
    """Extract and sanitize the main article HTML from a docs page."""
    article_match = re.search(
        r'<article[^>]*>(.*?)</article>', html, re.DOTALL | re.IGNORECASE
    )
    if article_match:
        body_html = article_match.group(1)
    else:
        main_match = re.search(
            r'<main[^>]*>(.*?)</main>', html, re.DOTALL | re.IGNORECASE
        )
        if main_match:
            body_html = main_match.group(1)
        else:
            body_match = re.search(
                r'<body[^>]*>(.*?)</body>', html, re.DOTALL | re.IGNORECASE
            )
            body_html = body_match.group(1) if body_match else html

    for pattern in (
        r'<script[^>]*>.*?</script>',
        r'<style[^>]*>.*?</style>',
        r'<svg[^>]*>.*?</svg>',
        r'<nav[^>]*>.*?</nav>',
    ):
        body_html = re.sub(pattern, "", body_html, flags=re.DOTALL | re.IGNORECASE)
    return body_html


async def _load_doc(url_or_path: str) -> dict[str, Any]:
    """Fetch, parse, and cache a docs page. Returns an error dict on failure."""
    url = _normalise_doc_url(url_or_path)
    if not url:
        return {"error": "Only tailscale.com URLs are supported", "input": url_or_path}

    cache_key = f"doc:{url}"
    cached_article = _cached(cache_key)
    if cached_article is not None:
        return cached_article

    result = await _http_get(url)
    if "error" in result:
        return result

    html = result.get("_html", "")
    if not html:
        return {"error": "Empty response", "url": url}

    body_html = _extract_body_html(html)
    cached_article = {
        "url": url,
        "title": _extract_title_from_html(html),
        "content": html_to_text(body_html),
        "outline": extract_outline(body_html),
    }
    _set_cache(cache_key, cached_article)
    return cached_article


def _paginate_doc_content(
    article: dict[str, Any],
    url_or_path: str,
    offset: int,
) -> dict[str, Any]:
    """Apply character paging to a loaded doc article."""
    full_content = article["content"]
    result_out: dict[str, Any] = {
        "url": article["url"],
        "title": article["title"],
        "content": full_content,
        "total_chars": len(full_content),
    }

    if offset > 0:
        start = max(0, offset)
        page_content = full_content[start : start + MAX_CONTENT_CHARS]
        remaining = max(0, len(full_content) - start - len(page_content))
        next_offset = start + len(page_content)
        result_out["content"] = page_content
        result_out["offset"] = start
        result_out["remaining_chars"] = remaining
        result_out["next_offset"] = next_offset if remaining else None
        result_out["continuation_note"] = (
            f"{remaining} characters remain; call get_doc(url_or_path={url_or_path!r}, "
            f"offset={next_offset}) for the next page."
            if remaining
            else "0 characters remain."
        )
        return result_out

    if len(full_content) > MAX_CONTENT_CHARS:
        remaining = len(full_content) - MAX_CONTENT_CHARS
        result_out["content"] = (
            full_content[:MAX_CONTENT_CHARS]
            + f"\n\n[... content truncated at {MAX_CONTENT_CHARS:,} characters ...]"
        )
        result_out["truncated"] = True
        result_out["remaining_chars"] = remaining
        result_out["next_offset"] = MAX_CONTENT_CHARS
        result_out["continuation_note"] = (
            f"{remaining} characters remain; call get_doc(url_or_path={url_or_path!r}, "
            f"offset={MAX_CONTENT_CHARS}) for the next page."
        )

    return result_out


def _normalise_list_prefix(prefix: str) -> str:
    """Normalise a list_docs prefix to a /docs/... path when using path_prefix."""
    normalized = prefix.strip()
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if not normalized.startswith("/docs"):
        normalized = f"/docs{normalized}"
    return normalized


def _doc_path_matches(path: str, prefix: str, path_prefix: bool) -> bool:
    if not prefix:
        return True
    if path_prefix:
        return path.lower().startswith(_normalise_list_prefix(prefix).lower())
    return prefix.lower() in path.lower()


def _normalise_doc_url(url_or_path: str) -> str:
    """Accept full URL, /docs/... path, or kb/NNNN/slug -> absolute docs URL."""
    stripped = url_or_path.strip()
    if stripped.startswith("http://"):
        stripped = "https://" + stripped[len("http://"):]
    if stripped.startswith("https://"):
        host = urlparse(stripped).hostname or ""
        if host != "tailscale.com" and not host.endswith(".tailscale.com"):
            return ""  # refuse non-Tailscale hosts
        return stripped
    if stripped.startswith("/"):
        return f"{DOCS_BASE}{stripped}"
    # bare kb/NNNN/slug or docs/... form
    return f"{DOCS_BASE}/{stripped}"


def _extract_title_from_html(html: str) -> str:
    """Pull <title> from HTML, with fallback."""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if m:
        return html_mod.unescape(re.sub(r"\s+", " ", m.group(1)).strip())
    return ""


def _strip_highlight_tags(text: str) -> str:
    """Remove <mark> and <em> highlight tags from search descriptions."""
    return re.sub(r"</?(?:mark|em)[^>]*>", "", text)


# ── Sitemap parsing ──────────────────────────────────────────────────────────


async def _fetch_sitemap() -> list[dict[str, str]]:
    """Fetch and parse sitemap.xml, return list of {path, url} for /docs/ URLs."""
    cached = _cached("sitemap")
    if cached is not None:
        return cached

    result = await _http_get(SITEMAP_URL)
    if "error" in result:
        return []

    html = result.get("_html", "")
    if not html:
        return []

    docs_urls: list[dict[str, str]] = []
    # Extract <loc> tags
    for m in re.finditer(
        r"<loc>(https://tailscale\.com/docs[^<]*)</loc>", html, re.IGNORECASE
    ):
        url = m.group(1)
        path = url.replace(DOCS_BASE, "")
        docs_urls.append({"path": path, "url": url})

    _set_cache("sitemap", docs_urls)
    return docs_urls


# ── Tools ────────────────────────────────────────────────────────────────────


@mcp.tool()
async def search_docs(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Search Tailscale documentation by keyword.

    Args:
        query: Search keywords or phrase.
        limit: Max results to return (default 5). Slices client-side from
               the 10 results the API returns.
    """
    cache_key = f"search:{query}"
    cached = _cached(cache_key)
    if cached is not None:
        return cached[:limit]

    result = await _http_get(SEARCH_URL, params={"q": query})
    if "error" in result:
        return [result]

    items = result.get("data", [])
    out = []
    for r in items:
        raw_desc = r.get("description", "")
        out.append(
            {
                "title": r.get("title", ""),
                "url": r.get("permalink", ""),
                "description": _strip_highlight_tags(raw_desc).strip(),
                "score": r.get("score", 0),
            }
        )

    _set_cache(cache_key, out)
    return out[:limit]


@mcp.tool()
async def get_doc(url_or_path: str, offset: int = 0) -> dict[str, Any]:
    """Fetch the full plain-text content of a Tailscale docs page.

    Accepts:
      - A full URL: https://tailscale.com/docs/features/exit-nodes
      - A /docs/... path: /docs/features/exit-nodes
      - A kb/NNNN/slug path: kb/1080/cli  (auto-redirected to /docs/...)

    Args:
        url_or_path: Full Tailscale URL or documentation path.
        offset: Optional character offset for retrieving a continuation page.

    Returns dict with url, title, content, and paging metadata. Long pages are
    limited to 12,000 characters; use next_offset (or offset) for continuation.
    """
    article = await _load_doc(url_or_path)
    if "error" in article:
        return article
    return _paginate_doc_content(article, url_or_path, offset)


@mcp.tool()
async def get_doc_outline(url_or_path: str, max_level: int = 3) -> dict[str, Any]:
    """Return the heading outline for a Tailscale docs page.

    Useful for skimming long pages before calling get_doc. Headings include
    level (1-3 by default), text, id, and anchor (#fragment).

    Args:
        url_or_path: Full Tailscale URL or documentation path.
        max_level: Include headings up to this level (default 3).
    """
    article = await _load_doc(url_or_path)
    if "error" in article:
        return article

    max_level = max(1, min(6, max_level))
    headings = [h for h in article["outline"] if h["level"] <= max_level]
    return {
        "url": article["url"],
        "title": article["title"],
        "headings": headings,
    }


@mcp.tool()
async def list_docs(
    prefix: str = "",
    limit: int = 50,
    path_prefix: bool = False,
) -> dict[str, Any]:
    """List available Tailscale documentation pages from the sitemap.

    Args:
        prefix: Filter docs paths. By default matches any substring in the path.
                When path_prefix is true, only paths starting with this prefix
                match (e.g. "reference/" or "/docs/reference/").
        limit: Max entries to return (default 50).
        path_prefix: When true, treat prefix as a path prefix instead of substring.
    """
    urls = await _fetch_sitemap()
    if not urls:
        return {"total": 0, "returned": 0, "has_more": False, "items": []}

    if prefix:
        urls = [
            u
            for u in urls
            if _doc_path_matches(u["path"], prefix, path_prefix)
        ]

    total = len(urls)
    items = [{"path": u["path"], "url": u["url"]} for u in urls[:limit]]
    return {
        "total": total,
        "returned": len(items),
        "has_more": total > len(items),
        "items": items,
    }


# ── Entrypoint ───────────────────────────────────────────────────────────────

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
