# Tailscale Docs Live MCP Server

An MCP (Model Context Protocol) server that gives AI agents live access to the
official Tailscale documentation at [tailscale.com/docs](https://tailscale.com/docs).
No scraping pipeline, no stale snapshots — every query hits the live site.

Unofficial. Not affiliated with or endorsed by Tailscale Inc.

## How it works

- **Search** uses Tailscale's own public search endpoint (`tailscale.com/api/search`).
- **Page content** is extracted from the server-rendered `<article>` HTML of each
  docs page and converted to readable plain text/Markdown with the Python stdlib
  (no BeautifulSoup). Tables become Markdown tables, links are preserved as
  absolute Markdown links, and `<pre>` code whitespace is preserved.
- **Inventory** comes from `sitemap.xml` (~600 docs pages).
- Results are cached in memory for 10 minutes to keep traffic polite.
- Only `tailscale.com` URLs are fetched; anything else is refused.

## Tools

| Tool | Description |
|---|---|
| `search_docs(query, limit=5)` | Full-text search of the docs. Returns title, URL, description, score. |
| `get_doc(url_or_path, offset=0)` | Read a docs page. Accepts a full URL, a `/docs/...` path, or a legacy `kb/NNNN/slug` path (follows the redirect). The default response is limited to 12,000 chars; pass `offset` to page through longer content. |
| `list_docs(prefix="", limit=50)` | Browse all docs pages from the sitemap, filtered by path substring (e.g. `exit-nodes`, `reference/`). |

## Install

```bash
git clone https://github.com/alfranli123/tailscale-docs-live-mcp.git
cd tailscale-docs-live-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Requires Python 3.10+. Dependencies: `mcp`, `httpx`.

## MCP client configuration

Any MCP client that supports stdio servers works. Generic shape:

```json
{
  "mcpServers": {
    "tailscale-docs-live": {
      "command": "/path/to/tailscale-docs-live-mcp/.venv/bin/python",
      "args": ["/path/to/tailscale-docs-live-mcp/server.py"]
    }
  }
}
```

## Notes

- Error conditions return `{"error": ..., "status": ...}` dicts; the server
  never crashes on bad input.
- Long pages (e.g. the CLI reference) return the first 12,000 characters with
  a notice and report continuation guidance; pass the suggested `offset` to
  retrieve the next slice from the cached full conversion.
- Search descriptions have Tailscale's `<mark>`/`<em>` highlight tags stripped.

## License

MIT
