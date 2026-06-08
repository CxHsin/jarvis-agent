from __future__ import annotations

import json
from html.parser import HTMLParser
from typing import Any

import httpx
from ddgs import DDGS

from jarvis.tools.base import Tool, ToolCallResult, ToolMeta

_MAX_SEARCH_SNIPPET_CHARS = 300
_MAX_FETCH_TEXT_CHARS = 8000


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text:
            self.parts.append(text)

    def text(self) -> str:
        return " ".join(self.parts)


class WebSearchTool(Tool):
    name = "web_search"
    description = "Search the web and return a compact list of result titles, snippets, and URLs."
    meta = ToolMeta(
        risk_level="read-only",
        always_on=True,
        default_timeout_seconds=20,
        search_hint="search web internet find results",
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
        },
        "required": ["query"],
    }

    async def execute(self, arguments: dict[str, Any]) -> ToolCallResult:
        query = str(arguments["query"])
        try:
            results = await _search_duckduckgo(query)
            payload = {"query": query, "results": results}
            return ToolCallResult(
                tool_name=self.name,
                status="success",
                content=json.dumps({"query": query, "results": results}, ensure_ascii=False),
                structured=payload,
                risk_level=self.meta.risk_level,
            )
        except Exception as exc:
            payload = {
                "query": query,
                "error": f"Search failed: {exc}",
            }
            return ToolCallResult(
                tool_name=self.name,
                status="error",
                content=json.dumps(
                    payload,
                    ensure_ascii=False,
                ),
                structured=payload,
                risk_level=self.meta.risk_level,
            )


class FetchUrlTool(Tool):
    name = "fetch_url"
    description = "Fetch a URL and return extracted text content."
    meta = ToolMeta(
        risk_level="read-only",
        always_on=True,
        default_timeout_seconds=20,
        search_hint="fetch url open webpage read page",
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "HTTP or HTTPS URL."},
        },
        "required": ["url"],
    }

    async def execute(self, arguments: dict[str, Any]) -> ToolCallResult:
        url = str(arguments["url"])
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
        text = _html_to_text(response.text)
        text = _truncate_text(text, _MAX_FETCH_TEXT_CHARS)
        payload = {"url": url, "status": response.status_code, "text": text}
        return ToolCallResult(
            tool_name=self.name,
            status="success",
            content=json.dumps(
                payload,
                ensure_ascii=False,
            ),
            structured=payload,
            risk_level=self.meta.risk_level,
        )


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text()


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 16] + "...[truncated]"


async def _search_duckduckgo(query: str) -> list[dict[str, str]]:
    def _run() -> list[dict[str, str]]:
        items = []
        with DDGS() as ddgs:
            for entry in ddgs.text(query, max_results=5):
                title = _truncate_text(str(entry.get("title", "")), 160)
                snippet = _truncate_text(str(entry.get("body", "")), _MAX_SEARCH_SNIPPET_CHARS)
                url = str(entry.get("href", ""))
                if not title and not url:
                    continue
                items.append({"title": title, "snippet": snippet, "url": url})
        return items

    return await _run_blocking(_run)


async def _run_blocking(func):
    import asyncio

    return await asyncio.to_thread(func)
