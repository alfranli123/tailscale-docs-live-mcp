#!/usr/bin/env python3
"""Smoke-test the MCP server over stdio."""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _parse_tool_blocks(result) -> list[dict]:
    return [json.loads(block.text) for block in result.content]


async def main() -> int:
    root = Path(__file__).resolve().parent.parent
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(root / "server.py")],
        cwd=str(root),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            tool_names = sorted(tool.name for tool in tools.tools)
            print("tools:", tool_names)

            search_result = await session.call_tool(
                "search_docs",
                arguments={"query": "exit nodes", "limit": 2},
            )
            search_payload = _parse_tool_blocks(search_result)
            print("search_docs count:", len(search_payload))
            print("search_docs first title:", search_payload[0]["title"])

            outline_result = await session.call_tool(
                "get_doc_outline",
                arguments={"url_or_path": "/docs/features/exit-nodes", "max_level": 3},
            )
            outline_payload = _parse_tool_blocks(outline_result)[0]
            print("get_doc_outline headings:", len(outline_payload.get("headings", [])))
            print("get_doc_outline first:", outline_payload.get("headings", [{}])[0].get("text"))

            doc_result = await session.call_tool(
                "get_doc",
                arguments={"url_or_path": "/docs/features/exit-nodes"},
            )
            doc_payload = _parse_tool_blocks(doc_result)[0]
            print("get_doc title:", doc_payload["title"])
            print("get_doc content chars:", len(doc_payload.get("content", "")))
            print("get_doc next_offset:", doc_payload.get("next_offset"))

            list_result = await session.call_tool(
                "list_docs",
                arguments={"prefix": "exit-nodes", "limit": 3},
            )
            list_payload = _parse_tool_blocks(list_result)[0]
            print("list_docs total:", list_payload.get("total"))
            print("list_docs returned:", list_payload.get("returned"))

    expected_tools = {"get_doc", "get_doc_outline", "list_docs", "search_docs"}
    if set(tool_names) != expected_tools:
        print("unexpected tools:", set(tool_names) ^ expected_tools, file=sys.stderr)
        return 1
    if (
        not search_payload
        or not outline_payload.get("headings")
        or not doc_payload.get("content")
        or not list_payload.get("items")
    ):
        print("tool calls returned empty payloads", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
