import time

import httpx
import pytest

import server
from server import (
    _TTLCache,
    _http_get,
    _normalise_doc_url,
    get_doc,
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

    async def test_list_docs_filters_by_prefix(self, httpx_mock):
        httpx_mock.add_response(
            text="""
            <urlset>
              <url><loc>https://tailscale.com/docs/features/exit-nodes</loc></url>
              <url><loc>https://tailscale.com/docs/install/linux</loc></url>
            </urlset>
            """
        )

        results = await list_docs(prefix="exit-nodes", limit=10)

        assert results == [
            {
                "path": "/docs/features/exit-nodes",
                "url": "https://tailscale.com/docs/features/exit-nodes",
            }
        ]
