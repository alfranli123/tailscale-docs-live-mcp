#!/usr/bin/env python3
"""
MCP server that provides live access to Tailscale documentation
(tailscale.com/docs) via the public search API, page HTML extraction,
and sitemap inventory.

Tools: search_docs, get_doc, list_docs
"""

import re
import time
import html as html_mod
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from mcp.server.fastmcp import FastMCP

# ── Configuration ────────────────────────────────────────────────────────────

SEARCH_URL = "https://tailscale.com/api/search"
SITEMAP_URL = "https://tailscale.com/sitemap.xml"
DOCS_BASE = "https://tailscale.com"
USER_AGENT = "tailscale-docs-live-mcp/0.1 (+https://github.com/alfranli123/tailscale-docs-live-mcp)"
REQUEST_TIMEOUT = 20  # seconds
CACHE_TTL = 600  # seconds (10 min)
MAX_CONTENT_CHARS = 12000

# ── Server instance ──────────────────────────────────────────────────────────

mcp = FastMCP(
    "tailscale-docs-live",
    instructions=(
        "Live Tailscale documentation via public search API and page extraction. "
        "Use search_docs to find docs, get_doc to read article content, "
        "and list_docs to browse the sitemap."
    ),
)

# ── Simple TTL cache ─────────────────────────────────────────────────────────

_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, val = entry
    if time.monotonic() - ts > CACHE_TTL:
        del _cache[key]
        return None
    return val


def _set_cache(key: str, val: Any) -> None:
    _cache[key] = (time.monotonic(), val)


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


async def _http_get(
    url: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    """GET a URL, return parsed JSON or an error dict."""
    client = await _get_client()
    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "json" in content_type:
            return resp.json()
        return {"_html": resp.text, "_status": resp.status_code}
    except httpx.HTTPStatusError as exc:
        return {
            "error": f"HTTP {exc.response.status_code} from {url}",
            "status": exc.response.status_code,
        }
    except httpx.RequestError as exc:
        return {"error": f"Request failed: {exc}"}
    except Exception as exc:
        return {"error": str(exc)}


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

    Returns dict with url, title, content. Returns error dict on 404 or failure.
    The default page is limited to 12,000 characters; use offset for continuation.
    """
    url = _normalise_doc_url(url_or_path)
    if not url:
        return {"error": "Only tailscale.com URLs are supported", "input": url_or_path}

    cache_key = f"doc:{url}"
    cached_article = _cached(cache_key)
    if cached_article is None:
        result = await _http_get(url)
        if "error" in result:
            return result

        html = result.get("_html", "")
        if not html:
            return {"error": "Empty response", "url": url}

        # Extract title
        title = _extract_title_from_html(html)

        # Extract <article> block (the main docs content)
        article_match = re.search(
            r'<article[^>]*>(.*?)</article>', html, re.DOTALL | re.IGNORECASE
        )
        if article_match:
            body_html = article_match.group(1)
        else:
            # Fallback: try <main> or just use the whole body
            main_match = re.search(
                r'<main[^>]*>(.*?)</main>', html, re.DOTALL | re.IGNORECASE
            )
            if main_match:
                body_html = main_match.group(1)
            else:
                # Use everything inside <body>
                body_match = re.search(
                    r'<body[^>]*>(.*?)</body>', html, re.DOTALL | re.IGNORECASE
                )
                body_html = body_match.group(1) if body_match else html

        # Strip <script> and <style> blocks before HTML-to-text conversion
        body_html = re.sub(
            r'<script[^>]*>.*?</script>', "", body_html, flags=re.DOTALL | re.IGNORECASE
        )
        body_html = re.sub(
            r'<style[^>]*>.*?</style>', "", body_html, flags=re.DOTALL | re.IGNORECASE
        )
        # Also strip SVG tags
        body_html = re.sub(
            r'<svg[^>]*>.*?</svg>', "", body_html, flags=re.DOTALL | re.IGNORECASE
        )
        # Strip nav tags too
        body_html = re.sub(
            r'<nav[^>]*>.*?</nav>', "", body_html, flags=re.DOTALL | re.IGNORECASE
        )

        cached_article = {
            "url": url,
            "title": title,
            "content": html_to_text(body_html),
        }
        _set_cache(cache_key, cached_article)

    full_content = cached_article["content"]
    result_out: dict[str, Any] = {
        "url": cached_article["url"],
        "title": cached_article["title"],
        "content": full_content,
    }

    if offset > 0:
        start = max(0, offset)
        page_content = full_content[start : start + MAX_CONTENT_CHARS]
        remaining = max(0, len(full_content) - start - len(page_content))
        result_out["content"] = page_content
        result_out["offset"] = start
        result_out["remaining_chars"] = remaining
        next_offset = start + len(page_content)
        result_out["continuation_note"] = (
            f"{remaining} characters remain; call get_doc(url_or_path={url_or_path!r}, "
            f"offset={next_offset}) for the next page."
            if remaining
            else "0 characters remain."
        )
        return result_out

    if len(full_content) > MAX_CONTENT_CHARS:
        result_out["content"] = (
            full_content[:MAX_CONTENT_CHARS]
            + f"\n\n[... content truncated at {MAX_CONTENT_CHARS:,} characters ...]"
        )
        result_out["truncated"] = True

    return result_out


@mcp.tool()
async def list_docs(prefix: str = "", limit: int = 50) -> list[dict[str, str]]:
    """List available Tailscale documentation pages from the sitemap.

    Args:
        prefix: Filter by substring in the doc path (e.g. "exit-nodes",
                "reference/", "install/"). Empty string returns all docs.
        limit: Max entries to return (default 50).
    """
    urls = await _fetch_sitemap()
    if not urls:
        return []

    if prefix:
        urls = [u for u in urls if prefix.lower() in u["path"].lower()]

    out = [{"path": u["path"], "url": u["url"]} for u in urls[:limit]]
    return out


# ── Entrypoint ───────────────────────────────────────────────────────────────

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
