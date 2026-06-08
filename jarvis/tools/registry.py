from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jarvis.tools.base import Tool, ToolMeta


@dataclass(frozen=True)
class ToolRecord:
    tool: Tool
    meta: ToolMeta


class ToolRegistry:
    def __init__(self) -> None:
        self._records: dict[str, ToolRecord] = {}

    def register(self, tool: Tool, *, meta: ToolMeta | None = None) -> None:
        self._records[tool.name] = ToolRecord(tool=tool, meta=meta or tool.meta)

    def get_tool(self, name: str) -> Tool | None:
        record = self._records.get(name)
        return record.tool if record else None

    def get_meta(self, name: str) -> ToolMeta | None:
        record = self._records.get(name)
        return record.meta if record else None

    def all_names(self) -> list[str]:
        return list(self._records.keys())

    def definitions(self, names: set[str] | None = None) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for name, record in self._records.items():
            if names is not None and name not in names:
                continue
            output.append(record.tool.to_definition())
        return output

    def always_on_names(self) -> set[str]:
        return {
            name for name, record in self._records.items() if record.meta.always_on
        }

    def deferred_names(self) -> set[str]:
        return set(self._records.keys()) - self.always_on_names()

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        include_always_on: bool = False,
    ) -> list[dict[str, Any]]:
        normalized = query.strip().lower()
        if not normalized:
            return []

        if normalized.startswith("select:"):
            names = [part.strip() for part in normalized[7:].split(",") if part.strip()]
            results = []
            for raw_name in names:
                for name, record in self._records.items():
                    if name.lower() != raw_name:
                        continue
                    if record.meta.always_on and not include_always_on:
                        continue
                    results.append(_search_result(name, record))
            return results[:limit]

        terms = [term for term in normalized.split() if term]
        scored: list[tuple[int, str, ToolRecord]] = []
        for name, record in self._records.items():
            if record.meta.always_on and not include_always_on:
                continue
            haystack = " ".join(
                part
                for part in (
                    name,
                    record.tool.description,
                    record.meta.search_hint or "",
                )
                if part
            ).lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((score, name, record))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [_search_result(name, record) for _, name, record in scored[:limit]]


def _search_result(name: str, record: ToolRecord) -> dict[str, Any]:
    return {
        "name": name,
        "summary": record.tool.description,
        "risk_level": record.meta.risk_level,
        "always_on": record.meta.always_on,
    }
