from __future__ import annotations

import json
import re
from pathlib import Path

from jarvis.memory.models import ContextTurn, HistoryEntry, PendingMemoryItem


class MarkdownMemoryStore:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._journal_dir = self._base_dir / "journal"
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._journal_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_defaults()

    @property
    def self_path(self) -> Path:
        return self._base_dir / "SELF.md"

    @property
    def memory_path(self) -> Path:
        return self._base_dir / "MEMORY.md"

    @property
    def recent_context_path(self) -> Path:
        return self._base_dir / "RECENT_CONTEXT.md"

    @property
    def history_path(self) -> Path:
        return self._base_dir / "HISTORY.md"

    @property
    def pending_path(self) -> Path:
        return self._base_dir / "PENDING.md"

    @property
    def pending_snapshot_path(self) -> Path:
        return self._base_dir / "PENDING.snapshot.md"

    def read_self(self) -> str:
        return self.self_path.read_text(encoding="utf-8")

    def write_self(self, content: str) -> None:
        self.self_path.write_text(content.strip() + "\n", encoding="utf-8")

    def read_memory(self) -> str:
        return self.memory_path.read_text(encoding="utf-8")

    def write_memory(self, content: str) -> None:
        self.memory_path.write_text(content.strip() + "\n", encoding="utf-8")

    def read_recent_context(self) -> str:
        return self.recent_context_path.read_text(encoding="utf-8")

    def write_recent_context(
        self,
        *,
        compression_lines: list[str],
        ongoing_thread_lines: list[str],
        recent_turns: list[ContextTurn],
    ) -> None:
        recent_turn_lines = [
            f"- [{item.created_at.strftime('%Y-%m-%d %H:%M:%S')}] {item.role.upper()}: {self._shorten(item.content, 240)}"
            for item in recent_turns
        ]
        if not recent_turn_lines:
            recent_turn_lines = ["- No recent turns yet."]
        content = "\n".join(
            [
                "# Recent Context",
                "",
                "## Compression",
                *self._normalize_lines(compression_lines, fallback="- No recent user summary yet."),
                "",
                "## Ongoing Threads",
                *self._normalize_lines(ongoing_thread_lines, fallback="- None."),
                "",
                "## Recent Turns",
                *recent_turn_lines,
                "",
            ]
        )
        self.recent_context_path.write_text(content, encoding="utf-8")

    def refresh_recent_turns(self, recent_turns: list[ContextTurn]) -> None:
        sections = self._read_recent_context_sections()
        self.write_recent_context(
            compression_lines=sections.get("Compression", ["- No recent user summary yet."]),
            ongoing_thread_lines=sections.get("Ongoing Threads", ["- None."]),
            recent_turns=recent_turns,
        )

    def append_history_entries(self, entries: list[HistoryEntry]) -> None:
        if not entries:
            return
        history_content = self.history_path.read_text(encoding="utf-8")
        history_append: list[str] = []
        journal_updates: dict[str, list[str]] = {}
        for entry in entries:
            marker = self._history_marker(entry)
            if marker in history_content:
                continue
            visible_line = f"[{entry.created_at.strftime('%Y-%m-%d %H:%M')}] {entry.text}"
            history_append.extend([marker, visible_line, ""])
            journal_updates.setdefault(entry.created_at.strftime("%Y-%m-%d"), []).extend([marker, visible_line, ""])
        if history_append:
            self.history_path.write_text(history_content.rstrip() + "\n\n" + "\n".join(history_append), encoding="utf-8")
        for journal_day, lines in journal_updates.items():
            path = self._journal_dir / f"{journal_day}.md"
            if not path.exists():
                path.write_text(f"# Journal {journal_day}\n\n", encoding="utf-8")
            existing = path.read_text(encoding="utf-8")
            path.write_text(existing.rstrip() + "\n\n" + "\n".join(lines), encoding="utf-8")

    def append_pending_items(self, items: list[PendingMemoryItem]) -> None:
        if not items:
            return
        existing = self.pending_path.read_text(encoding="utf-8")
        append_lines: list[str] = []
        for item in items:
            marker = self._pending_marker(item)
            if marker in existing:
                continue
            append_lines.extend(
                [
                    marker,
                    f"- [{item.tag}] {item.text}",
                    "",
                ]
            )
        if append_lines:
            self.pending_path.write_text(existing.rstrip() + "\n\n" + "\n".join(append_lines), encoding="utf-8")

    def list_pending_items(self) -> list[PendingMemoryItem]:
        text = self.pending_path.read_text(encoding="utf-8")
        items: list[PendingMemoryItem] = []
        lines = text.splitlines()
        for index, line in enumerate(lines):
            match = re.fullmatch(r"<!-- pending:(.+) -->", line.strip())
            if not match:
                continue
            payload = json.loads(match.group(1))
            visible = lines[index + 1].strip() if index + 1 < len(lines) else ""
            visible = visible.removeprefix("- ").strip()
            if visible.startswith("[") and "] " in visible:
                _, visible = visible.split("] ", 1)
            payload["text"] = visible or payload.get("text", "")
            items.append(PendingMemoryItem.model_validate(payload))
        return items

    def snapshot_pending(self) -> None:
        self.pending_snapshot_path.write_text(self.pending_path.read_text(encoding="utf-8"), encoding="utf-8")

    def restore_pending_snapshot(self) -> None:
        if self.pending_snapshot_path.exists():
            self.pending_path.write_text(self.pending_snapshot_path.read_text(encoding="utf-8"), encoding="utf-8")

    def clear_pending(self) -> None:
        self.pending_path.write_text("# Pending Memory\n\n", encoding="utf-8")

    def delete_pending_snapshot(self) -> None:
        if self.pending_snapshot_path.exists():
            self.pending_snapshot_path.unlink()

    def _ensure_defaults(self) -> None:
        defaults = {
            self.self_path: "\n".join(
                [
                    "# Self Model",
                    "",
                    "## Role",
                    "- Jarvis is a pragmatic personal assistant.",
                    "",
                    "## Relationship",
                    "- Jarvis helps the user with practical tasks and continuity.",
                    "",
                ]
            ),
            self.memory_path: "\n".join(
                [
                    "# Memory",
                    "",
                    "## Identity",
                    "- None yet.",
                    "",
                    "## Preferences",
                    "- None yet.",
                    "",
                    "## Key Info",
                    "- None yet.",
                    "",
                    "## Health",
                    "- None yet.",
                    "",
                    "## Requested Memory",
                    "- None yet.",
                    "",
                    "## Corrections",
                    "- None yet.",
                    "",
                ]
            ),
            self.recent_context_path: "\n".join(
                [
                    "# Recent Context",
                    "",
                    "## Compression",
                    "- No recent user summary yet.",
                    "",
                    "## Ongoing Threads",
                    "- None.",
                    "",
                    "## Recent Turns",
                    "- No recent turns yet.",
                    "",
                ]
            ),
            self.history_path: "# History\n\n",
            self.pending_path: "# Pending Memory\n\n",
        }
        for path, content in defaults.items():
            if not path.exists():
                path.write_text(content, encoding="utf-8")

    def _read_recent_context_sections(self) -> dict[str, list[str]]:
        sections: dict[str, list[str]] = {}
        current: str | None = None
        for line in self.recent_context_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                current = line[3:].strip()
                sections.setdefault(current, [])
                continue
            if current and line and not line.startswith("# "):
                sections[current].append(line)
        return sections

    def _normalize_lines(self, lines: list[str], *, fallback: str) -> list[str]:
        normalized = [line for line in lines if line.strip()]
        return normalized or [fallback]

    def _history_marker(self, entry: HistoryEntry) -> str:
        return f'<!-- consolidation:{json.dumps(entry.message_ids, ensure_ascii=False)}:history_entry -->'

    def _pending_marker(self, item: PendingMemoryItem) -> str:
        payload = item.model_dump(mode="json")
        return f"<!-- pending:{json.dumps(payload, ensure_ascii=False)} -->"

    def _shorten(self, text: str, limit: int) -> str:
        clean = " ".join(text.split())
        if len(clean) <= limit:
            return clean
        return clean[: limit - 3].rstrip() + "..."
