from __future__ import annotations

from datetime import datetime, timedelta

from jarvis.memory.markdown_store import MarkdownMemoryStore
from jarvis.memory.metadata import FileMemoryMetadataStore
from jarvis.memory.models import PendingMemoryItem


class MemoryOptimizer:
    _SECTION_ORDER = [
        "Identity",
        "Preferences",
        "Key Info",
        "Health",
        "Requested Memory",
        "Corrections",
    ]

    _TAG_TO_SECTION = {
        "identity": "Identity",
        "preference": "Preferences",
        "key_info": "Key Info",
        "health_long_term": "Health",
        "requested_memory": "Requested Memory",
        "correction": "Corrections",
    }

    def __init__(
        self,
        *,
        markdown_store: MarkdownMemoryStore,
        metadata_store: FileMemoryMetadataStore,
        enabled: bool,
        interval_seconds: int,
    ) -> None:
        self._markdown_store = markdown_store
        self._metadata_store = metadata_store
        self._enabled = enabled
        self._interval_seconds = max(1, interval_seconds)

    def should_run(self, occurred_at: datetime) -> bool:
        if not self._enabled:
            return False
        last_run = self._metadata_store.get_last_optimizer_run_at()
        if last_run is None:
            return True
        return occurred_at - last_run >= timedelta(seconds=self._interval_seconds)

    def optimize(self, occurred_at: datetime) -> tuple[bool, str]:
        if not self._enabled:
            return False, "memory optimizer disabled"
        if not self.should_run(occurred_at):
            return False, "memory optimizer not due yet"
        items = self._markdown_store.list_pending_items()
        if not items:
            self._metadata_store.set_last_optimizer_run_at(occurred_at)
            return False, "no pending memory to optimize"
        self._markdown_store.snapshot_pending()
        try:
            updated_memory = self._merge_memory_document(self._markdown_store.read_memory(), items)
            updated_self = self._merge_self_document(self._markdown_store.read_self(), items)
            self._markdown_store.write_memory(updated_memory)
            self._markdown_store.write_self(updated_self)
            self._markdown_store.clear_pending()
            self._markdown_store.delete_pending_snapshot()
            self._metadata_store.set_last_optimizer_run_at(occurred_at)
        except Exception:
            self._markdown_store.restore_pending_snapshot()
            raise
        return True, f"optimized {len(items)} pending memory items"

    def _merge_memory_document(self, current: str, items: list[PendingMemoryItem]) -> str:
        sections = self._parse_sections(current)
        for item in items:
            section = self._TAG_TO_SECTION.get(item.tag, "Key Info")
            sections.setdefault(section, [])
            sections[section].append(self._normalize_memory_line(item.text))
            sections[section] = self._dedupe_and_cap(sections[section], limit=12)
        lines = ["# Memory", ""]
        for section in self._SECTION_ORDER:
            lines.append(f"## {section}")
            values = sections.get(section, [])
            if values:
                lines.extend(f"- {value}" for value in values)
            else:
                lines.append("- None yet.")
            lines.append("")
        return "\n".join(lines)

    def _merge_self_document(self, current: str, items: list[PendingMemoryItem]) -> str:
        sections = self._parse_sections(current)
        sections.setdefault("Role", ["Jarvis is a pragmatic personal assistant."])
        sections.setdefault("Relationship", ["Jarvis helps the user with practical tasks and continuity."])
        relationship_updates = [
            self._normalize_memory_line(item.text)
            for item in items
            if "jarvis" in item.text.lower() or "assistant" in item.text.lower() or "you are" in item.text.lower()
        ]
        if relationship_updates:
            sections["Relationship"] = self._dedupe_and_cap(
                sections["Relationship"] + relationship_updates,
                limit=8,
            )
        lines = ["# Self Model", ""]
        for section in ("Role", "Relationship"):
            lines.append(f"## {section}")
            values = sections.get(section, [])
            lines.extend(f"- {value}" for value in values)
            lines.append("")
        return "\n".join(lines)

    def _parse_sections(self, content: str) -> dict[str, list[str]]:
        sections: dict[str, list[str]] = {}
        current: str | None = None
        for line in content.splitlines():
            if line.startswith("## "):
                current = line[3:].strip()
                sections.setdefault(current, [])
                continue
            if current and line.startswith("- "):
                value = line[2:].strip()
                if value and value != "None yet.":
                    sections[current].append(value)
        return sections

    def _normalize_memory_line(self, text: str) -> str:
        return " ".join(text.split())

    def _dedupe_and_cap(self, values: list[str], *, limit: int) -> list[str]:
        seen: set[str] = set()
        deduped: list[str] = []
        for value in values:
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(value)
        return deduped[-limit:]
