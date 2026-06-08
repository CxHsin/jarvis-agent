from dataclasses import dataclass

from app.memory_normalizer import (
    MemoryEntry,
    classify_user_memory_entry,
    format_memory_entries,
    normalize_text,
    parse_memory_entries,
)
from app.memory_store import MemorySnapshot
from app.self_model import (
    apply_interaction_style_updates,
    format_self_model,
    parse_self_model,
)


_IDENTITY_MARKERS = (
    "i am ",
    "i'm ",
    "my name is ",
    "as an ai",
    "deepseek",
    "chatgpt",
    "jarvis",
    "i don't have memory",
    "i do not have memory",
    "i don't have long-term memory",
    "i do not have long-term memory",
    "every conversation is a fresh start",
    "each conversation is a fresh start",
    "i can't remember past conversations",
    "i cannot remember past conversations",
)


@dataclass(frozen=True)
class MemoryWritePlan:
    self_text: str | None
    memory_text: str | None
    pending_text: str | None
    history_entry_text: str | None


class MemoryPolicy:
    _MAX_PENDING_LINES = 10
    _MAX_MEMORY_LINES = 20
    _DIRECT_MEMORY_TAGS = frozenset({"identity", "requested_memory"})
    _PENDING_TAGS = frozenset({"identity", "preference", "requested_memory"})

    def build_memory_write_plan(
        self,
        *,
        memory_snapshot: MemorySnapshot | None,
        user_text: str,
        memory_candidates: list[MemoryEntry] | None = None,
    ) -> MemoryWritePlan:
        normalized_user_text = normalize_text(user_text)
        if not normalized_user_text and not memory_candidates:
            return MemoryWritePlan(
                self_text=None,
                memory_text=None,
                pending_text=None,
                history_entry_text=None,
            )

        existing_memory_entries = parse_memory_entries(
            "" if memory_snapshot is None else memory_snapshot.memory_text
        )
        existing_pending_entries = parse_memory_entries(
            "" if memory_snapshot is None else memory_snapshot.pending_text
        )

        candidate = classify_user_memory_entry(normalized_user_text)
        memory_text = None
        pending_text = None
        self_text = None
        if candidate is not None:
            if candidate.tag in self._DIRECT_MEMORY_TAGS:
                updated_memory_entries = self._upsert_entry(
                    existing_memory_entries,
                    candidate,
                    limit=self._MAX_MEMORY_LINES,
                )
                memory_text = format_memory_entries(updated_memory_entries)
            elif candidate.tag in self._PENDING_TAGS:
                if self._contains_entry(existing_pending_entries, candidate):
                    updated_memory_entries = self._upsert_entry(
                        existing_memory_entries,
                        candidate,
                        limit=self._MAX_MEMORY_LINES,
                    )
                    memory_text = format_memory_entries(updated_memory_entries)
                    remaining_pending_entries = self._remove_entry(
                        existing_pending_entries,
                        candidate,
                    )
                    pending_text = format_memory_entries(remaining_pending_entries)
                    self_text = self._maybe_update_self_text(
                        memory_snapshot=memory_snapshot,
                        promoted_entry=candidate,
                    )
                else:
                    updated_pending_entries = self._upsert_entry(
                        existing_pending_entries,
                        candidate,
                        limit=self._MAX_PENDING_LINES,
                    )
                    pending_text = format_memory_entries(updated_pending_entries)

        plugin_candidates = memory_candidates or []
        if plugin_candidates:
            updated_memory_entries = existing_memory_entries
            last_style_update: str | None = None
            for plugin_candidate in plugin_candidates:
                updated_memory_entries = self._upsert_entry(
                    updated_memory_entries,
                    plugin_candidate,
                    limit=self._MAX_MEMORY_LINES,
                )
                style_update = _preference_to_self_style(plugin_candidate)
                if style_update is not None:
                    last_style_update = style_update
            memory_text = format_memory_entries(updated_memory_entries)
            if last_style_update is not None:
                model = parse_self_model("" if memory_snapshot is None else memory_snapshot.self_text)
                updated = apply_interaction_style_updates(model, [last_style_update])
                self_text = format_self_model(updated)

        history_entry_text = f"User: {normalized_user_text}"
        return MemoryWritePlan(
            self_text=self_text,
            memory_text=memory_text,
            pending_text=pending_text,
            history_entry_text=history_entry_text,
        )

    def build_memory_sections(self, snapshot: MemorySnapshot | None) -> list[str]:
        if snapshot is None:
            return []

        sections: list[str] = []
        if snapshot.self_text.strip():
            sections.append(_format_memory_section("SELF.md", snapshot.self_text))

        user_sections: list[str] = []
        if snapshot.memory_text.strip():
            user_sections.append(_format_memory_section("MEMORY.md", snapshot.memory_text))
        if snapshot.recent_context_text.strip():
            user_sections.append(
                _format_memory_section("RECENT_CONTEXT.md", snapshot.recent_context_text)
            )
        if user_sections:
            sections.append(
                "\n\n".join(
                    [
                        (
                            "Trusted user context:\n"
                            "- The following memory files are trusted context derived from prior user interactions.\n"
                            "- When the user asks about continuity, prior topics, or whether you remember previous exchanges, prefer this context over generic disclaimers.\n"
                            "- Do not claim the conversation is always a fresh start if the trusted context shows prior interactions."
                        ),
                        *user_sections,
                    ]
                )
            )
        return sections

    def _maybe_update_self_text(
        self,
        *,
        memory_snapshot: MemorySnapshot | None,
        promoted_entry: MemoryEntry,
    ) -> str | None:
        style_update = _preference_to_self_style(promoted_entry)
        if style_update is None:
            return None

        model = parse_self_model("" if memory_snapshot is None else memory_snapshot.self_text)
        updated = apply_interaction_style_updates(model, [style_update])
        return format_self_model(updated)

    def should_replay_assistant_text(self, assistant_text: str) -> bool:
        text = assistant_text.strip()
        if not text:
            return False

        lowered = text.casefold()
        return not any(marker in lowered for marker in _IDENTITY_MARKERS)

    def _contains_entry(self, entries: list[MemoryEntry], candidate: MemoryEntry) -> bool:
        return any(entry.canonical_key == candidate.canonical_key for entry in entries)

    def _upsert_entry(
        self,
        entries: list[MemoryEntry],
        candidate: MemoryEntry,
        *,
        limit: int,
    ) -> list[MemoryEntry]:
        updated = [entry for entry in entries if entry.canonical_key != candidate.canonical_key]
        updated.append(candidate)
        return updated[-limit:]

    def _remove_entry(
        self,
        entries: list[MemoryEntry],
        candidate: MemoryEntry,
    ) -> list[MemoryEntry]:
        return [entry for entry in entries if entry.canonical_key != candidate.canonical_key]


def _preference_to_self_style(entry: MemoryEntry) -> str | None:
    if entry.tag != "preference":
        return None

    text = entry.display_text.strip()
    if text.casefold().startswith("i prefer "):
        return text[len("I prefer ") :].strip().capitalize()
    return None


def _format_memory_section(title: str, content: str) -> str:
    return f"[{title}]\n{content.strip()}"
