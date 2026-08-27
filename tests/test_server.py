import time

import httpx
import pytest

import server
from server import (
    _TTLCache,
    _doc_path_matches,
    _http_get,
    _normalise_doc_url,
    _normalise_list_prefix,
    extract_outline,
    get_doc,
    get_doc_outline,
    html_to_text,
    list_docs,
    search_docs,
)


class TestTTLCache:
    def test_get_returns_none_for_missing_key(self):
        cache = _TTLCache(max_entries=2, ttl_seconds=60)
        assert cache.get("missing") is None

    def test_set_and_get_round_trip(self):
        cache = _TTLCache(max_entries=2, ttl_seconds=60)
        cache.set("key", {"value": 1})
        assert cache.get("key") == {"value": 1}

    def test_expired_entries_are_evicted(self):
        cache = _TTLCache(max_entries=2, ttl_seconds=0.01)
        cache.set("key", "value")
        time.sleep(0.02)
        assert cache.get("key") is None

    def test_lru_evicts_oldest_entry(self):
        cache = _TTLCache(max_entries=2, ttl_seconds=60)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.get("a")
        cache.set("c", 3)
        assert cache.get("a") == 1
        assert cache.get("b") is None
        assert cache.get("c") == 3


class TestHtmlToText:
    def test_converts_headings_links_and_pre(self):
        html = """
        <h2>Setup</h2>
        <p>Read the <a href="/docs/features/exit-nodes">exit nodes guide</a>.</p>
        <pre>tailscale up --exit-node=auto</pre>
        """
        text = html_to_text(html)
        assert "## Setup" in text
        assert "[exit nodes guide](https://tailscale.com/docs/features/exit-nodes)" in text
        assert "tailscale up --exit-node=auto" in text
        assert "```" in text

    def test_converts_tables(self):
        html = """
        <table>
          <tr><th>Plan</th><th>Exit nodes</th></tr>
          <tr><td>Free</td><td>Yes</td></tr>
        </table>
        """
        text = html_to_text(html)
        assert "| Plan | Exit nodes |" in text
        assert "---|" in text
        assert "| Free | Yes |" in text


class TestExtractOutline:
    def test_extracts_heading_levels_and_ids(self):
        html = """
        <h1>Overview</h1>
        <h2 id="setup">Setup</h2>
        <h3 id="linux">Linux</h3>
        <h4 id="ignored">Too deep</h4>
        """
        outline = extract_outline(html, max_level=3)
        assert outline == [
            {"level": 1, "text": "Overview", "id": "overview", "anchor": "#overview"},
            {"level": 2, "text": "Setup", "id": "setup", "anchor": "#setup"},
            {"level": 3, "text": "Linux", "id": "linux", "anchor": "#linux"},
        ]


class TestListDocsFiltering:
    def test_path_prefix_normalisation(self):
        assert _normalise_list_prefix("reference/") == "/docs/reference/"
        assert _normalise_list_prefix("/docs/install/") == "/docs/install/"

    @pytest.mark.parametrize(
        ("path", "prefix", "path_prefix", "expected"),
        [
            ("/docs/reference/cli", "reference/", True, True),
            ("/docs/features/exit-nodes", "reference/", True, False),
            ("/docs/features/exit-nodes", "exit-nodes", False, True),
            ("/docs/install/linux", "exit-nodes", False, False),
        ],
    )
    def test_doc_path_matches(self, path, prefix, path_prefix, expected):
        assert _doc_path_matches(path, prefix, path_prefix) is expected


class TestNormaliseDocUrl:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("/docs/features/exit-nodes", "https://tailscale.com/docs/features/exit-nodes"),
            ("docs/features/exit-nodes", "https://tailscale.com/docs/features/exit-nodes"),
            (
                "https://tailscale.com/docs/features/exit-nodes",
                "https://tailscale.com/docs/features/exit-nodes",
            ),
            (
                "http://tailscale.com/docs/features/exit-nodes",
                "https://tailscale.com/docs/features/exit-nodes",
            ),
            ("https://example.com/docs/nope", ""),
        ],
    )
    def test_normalises_supported_urls(self, value, expected):
        assert _normalise_doc_url(value) == expected


@pytest.mark.asyncio
class TestHttpGet:
    async def test_retries_transient_http_errors(self, httpx_mock):
        httpx_mock.add_response(status_code=503)
        httpx_mock.add_response(status_code=503)
        httpx_mock.add_response(json={"status": "ok", "data": []})

        result = await _http_get(server.SEARCH_URL, params={"q": "exit nodes"})

        assert result == {"status": "ok", "data": []}
        assert len(httpx_mock.get_requests()) == 3

    async def test_respects_retry_after_header(self, httpx_mock, monkeypatch):
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(server.asyncio, "sleep", fake_sleep)
        httpx_mock.add_response(status_code=429, headers={"Retry-After": "2"})
        httpx_mock.add_response(json={"status": "ok", "data": []})

        result = await _http_get(server.SEARCH_URL, params={"q": "exit nodes"})

        assert result == {"status": "ok", "data": []}
        assert sleeps == [2.0]

    async def test_does_not_retry_client_errors(self, httpx_mock):
        httpx_mock.add_response(status_code=404)

        result = await _http_get("https://tailscale.com/docs/missing")

        assert result["status"] == 404
        assert len(httpx_mock.get_requests()) == 1

    async def test_retries_request_errors(self, httpx_mock, monkeypatch):
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(server.asyncio, "sleep", fake_sleep)
        httpx_mock.add_exception(httpx.ConnectError("connection reset"))
        httpx_mock.add_response(json={"status": "ok", "data": []})

        result = await _http_get(server.SEARCH_URL, params={"q": "exit nodes"})

        assert result == {"status": "ok", "data": []}
        assert len(httpx_mock.get_requests()) == 2
        assert sleeps == [1.0]


@pytest.mark.asyncio
class TestTools:
    async def test_search_docs_returns_parsed_results(self, httpx_mock):
        httpx_mock.add_response(
            json={
                "status": "ok",
                "query": "exit nodes",
                "data": [
                    {
                        "title": "Exit nodes",
                        "description": "Use <mark>exit</mark> <mark>nodes</mark>.",
                        "permalink": "https://tailscale.com/docs/features/exit-nodes",
                        "score": 8.2,
                    }
                ],
            }
        )

        results = await search_docs("exit nodes", limit=1)

        assert results == [
            {
                "title": "Exit nodes",
                "url": "https://tailscale.com/docs/features/exit-nodes",
                "description": "Use exit nodes.",
                "score": 8.2,
            }
        ]

    async def test_get_doc_extracts_article_content(self, httpx_mock):
        httpx_mock.add_response(
            text="""
            <html>
              <head><title>Exit nodes · Tailscale Docs</title></head>
              <body>
                <article>
                  <h2>Exit nodes</h2>
                  <p>Route all traffic through a device.</p>
                </article>
              </body>
            </html>
            """
        )

        result = await get_doc("/docs/features/exit-nodes")

        assert result["title"] == "Exit nodes · Tailscale Docs"
        assert "## Exit nodes" in result["content"]
        assert "Route all traffic through a device." in result["content"]

    async def test_get_doc_includes_next_offset_when_truncated(self, httpx_mock):
        long_body = "x" * 15000
        httpx_mock.add_response(
            text=f"""
            <html>
              <head><title>Long page · Tailscale Docs</title></head>
              <body><article><p>{long_body}</p></article></body>
            </html>
            """
        )

        result = await get_doc("/docs/features/long-page")

        assert result["truncated"] is True
        assert result["next_offset"] == server.MAX_CONTENT_CHARS
        assert result["remaining_chars"] == 15000 - server.MAX_CONTENT_CHARS

    async def test_get_doc_outline_returns_headings(self, httpx_mock):
        httpx_mock.add_response(
            text="""
            <html>
              <head><title>Exit nodes · Tailscale Docs</title></head>
              <body>
                <article>
                  <h1>Exit nodes</h1>
                  <h2 id="benefits">Benefits</h2>
                  <h3 id="setup">Setup</h3>
                </article>
              </body>
            </html>
            """
        )

        result = await get_doc_outline("/docs/features/exit-nodes")

        assert result["title"] == "Exit nodes · Tailscale Docs"
        assert result["headings"] == [
            {"level": 1, "text": "Exit nodes", "id": "exit-nodes", "anchor": "#exit-nodes"},
            {"level": 2, "text": "Benefits", "id": "benefits", "anchor": "#benefits"},
            {"level": 3, "text": "Setup", "id": "setup", "anchor": "#setup"},
        ]

    async def test_list_docs_returns_metadata(self, httpx_mock):
        httpx_mock.add_response(
            text="""
            <urlset>
              <url><loc>https://tailscale.com/docs/features/exit-nodes</loc></url>
              <url><loc>https://tailscale.com/docs/features/exit-nodes/setup</loc></url>
              <url><loc>https://tailscale.com/docs/install/linux</loc></url>
            </urlset>
            """
        )

        results = await list_docs(prefix="exit-nodes", limit=1)

        assert results == {
            "total": 2,
            "returned": 1,
            "has_more": True,
            "items": [
                {
                    "path": "/docs/features/exit-nodes",
                    "url": "https://tailscale.com/docs/features/exit-nodes",
                }
            ],
        }

    async def test_list_docs_path_prefix_filter(self, httpx_mock):
        httpx_mock.add_response(
            text="""
            <urlset>
              <url><loc>https://tailscale.com/docs/reference/cli</loc></url>
              <url><loc>https://tailscale.com/docs/features/exit-nodes</loc></url>
            </urlset>
            """
        )

        results = await list_docs(prefix="reference/", limit=10, path_prefix=True)

        assert results["total"] == 1
        assert results["items"][0]["path"] == "/docs/reference/cli"
