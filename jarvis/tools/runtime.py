from __future__ import annotations

import json
from typing import Any

from jarvis.tools.base import Tool, ToolCallRequest, ToolCallResult, ToolMeta, error_result
from jarvis.tools.executor import ToolExecutor
from jarvis.tools.registry import ToolRegistry


class ToolRuntime:
    def __init__(self, registry: ToolRegistry, executor: ToolExecutor) -> None:
        self._registry = registry
        self._executor = executor

    def definitions(self, unlocked_tools: set[str] | None = None) -> list[dict[str, Any]]:
        names = self.visible_tool_names(unlocked_tools or set())
        return self._registry.definitions(names)

    def visible_tool_names(self, unlocked_tools: set[str] | None = None) -> set[str]:
        return self._registry.always_on_names() | (unlocked_tools or set())

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return self._registry.search(query, limit=limit)

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        chat_id: str,
        unlocked_tools: set[str] | None = None,
    ) -> ToolCallResult:
        request = ToolCallRequest(
            call_id=f"{chat_id}:{name}",
            tool_name=name,
            arguments=arguments,
            chat_id=chat_id,
            visible_tools=tuple(sorted(self._registry.always_on_names())),
            unlocked_tools=tuple(sorted(unlocked_tools or set())),
        )
        result = await self._executor.execute(request)
        if name == "tool_search" and result.success:
            payload = result.structured or {}
            selected = payload.get("selected_names", [])
            if not selected and isinstance(arguments.get("query"), str):
                query = str(arguments["query"]).strip().lower()
                if query.startswith("select:"):
                    selected = [
                        item["name"]
                        for item in self.search(query, limit=20)
                    ]
            result.structured = {
                **(result.structured or {}),
                "selected_names": selected,
            }
        return result

    def meta(self, name: str) -> ToolMeta | None:
        return self._registry.get_meta(name)


class ToolSearchTool(Tool):
    name = "tool_search"
    description = "Search available deferred tools and optionally select tools for this turn."
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query or selection syntax like select:write_file.",
            },
        },
        "required": ["query"],
    }
    meta = ToolMeta(
        risk_level="read-only",
        always_on=True,
        default_timeout_seconds=10,
        search_hint="discover tools select deferred hidden tools",
    )

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def execute(self, arguments: dict[str, Any]) -> ToolCallResult:
        query = str(arguments["query"])
        results = self._registry.search(query)
        selected_names: list[str] = []
        normalized = query.strip().lower()
        if normalized.startswith("select:"):
            selected_names = [item["name"] for item in results]
        payload = {
            "query": query,
            "matches": results,
            "selected_names": selected_names,
        }
        return ToolCallResult(
            tool_name=self.name,
            status="success",
            content=json.dumps(payload, ensure_ascii=False),
            structured=payload,
            risk_level=self.meta.risk_level,
        )
