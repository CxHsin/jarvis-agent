from __future__ import annotations

import logging

from jarvis.memory.context import ContextStore
from jarvis.memory.embeddings import EmbeddingProvider
from jarvis.memory.markdown_store import MarkdownMemoryStore
from jarvis.memory.metadata import FileMemoryMetadataStore
from jarvis.memory.models import ContextTurn, HistoryEntry, PendingMemoryItem, TurnCommitted, VectorMemoryRecord
from jarvis.memory.vector_store import VectorStore

logger = logging.getLogger(__name__)


class MemoryConsolidator:
    def __init__(
        self,
        *,
        context_store: ContextStore,
        markdown_store: MarkdownMemoryStore,
        metadata_store: FileMemoryMetadataStore,
        embedding_provider: EmbeddingProvider,
        vector_store: VectorStore,
        keep_count: int,
    ) -> None:
        self._context_store = context_store
        self._markdown_store = markdown_store
        self._metadata_store = metadata_store
        self._embedding_provider = embedding_provider
        self._vector_store = vector_store
        self._min_new_messages = max(5, keep_count // 2)

    def on_turn_committed(self, turn: TurnCommitted) -> bool:
        self._markdown_store.refresh_recent_turns(self._context_store.get_recent_turns(turn.session_key))
        if not self.should_consolidate(turn.session_key):
            return False
        return self.consolidate(turn.session_key)

    def should_consolidate(self, session_key: str) -> bool:
        last_sequence = self._metadata_store.get_last_consolidated_sequence(session_key)
        return len(self._context_store.get_turns_since(session_key, last_sequence)) >= self._min_new_messages

    def consolidate(self, session_key: str) -> bool:
        last_sequence = self._metadata_store.get_last_consolidated_sequence(session_key)
        batch = self._context_store.get_turns_since(session_key, last_sequence)
        if not batch:
            return False
        source_ref = f"{session_key}:{batch[0].sequence}-{batch[-1].sequence}"
        if self._metadata_store.has_processed_source_ref(source_ref):
            return False
        history_entries = self._build_history_entries(source_ref, batch)
        pending_items = self._build_pending_items(source_ref, batch)
        self._markdown_store.append_history_entries(history_entries)
        self._markdown_store.append_pending_items(pending_items)
        self._markdown_store.write_recent_context(
            compression_lines=self._build_compression_lines(batch),
            ongoing_thread_lines=self._build_thread_lines(batch),
            recent_turns=self._context_store.get_recent_turns(session_key),
        )
        self._metadata_store.mark_processed_source_ref(source_ref)
        self._metadata_store.set_last_consolidated_sequence(session_key, batch[-1].sequence)
        try:
            self._ingest_vectors(session_key, history_entries, pending_items)
        except Exception:
            logger.exception("vector ingestion failed for session_key=%s source_ref=%s", session_key, source_ref)
        return True

    def _build_history_entries(self, source_ref: str, batch: list[ContextTurn]) -> list[HistoryEntry]:
        entries: list[HistoryEntry] = []
        for turn in batch:
            label = "User" if turn.role == "user" else "Assistant" if turn.role == "assistant" else "Tool"
            entries.append(
                HistoryEntry(
                    source_ref=source_ref,
                    message_ids=[turn.message_id],
                    text=f"{label}: {self._shorten(turn.content, 220)}",
                    created_at=turn.created_at,
                )
            )
        return entries

    def _build_pending_items(self, source_ref: str, batch: list[ContextTurn]) -> list[PendingMemoryItem]:
        items: list[PendingMemoryItem] = []
        seen: set[tuple[str, str]] = set()
        for turn in batch:
            if turn.role != "user":
                continue
            for tag in self._infer_tags(turn.content):
                normalized = (tag, turn.content.strip().lower())
                if normalized in seen:
                    continue
                seen.add(normalized)
                items.append(
                    PendingMemoryItem(
                        item_id=f"{source_ref}:{turn.sequence}:{tag}",
                        source_ref=source_ref,
                        tag=tag,
                        text=self._shorten(" ".join(turn.content.split()), 220),
                        created_at=turn.created_at,
                    )
                )
        return items

    def _build_compression_lines(self, batch: list[ContextTurn]) -> list[str]:
        user_turns = [item for item in batch if item.role == "user"]
        if not user_turns:
            return ["- No recent user summary yet."]
        return [f"- {self._shorten(turn.content, 180)}" for turn in user_turns[-4:]]

    def _build_thread_lines(self, batch: list[ContextTurn]) -> list[str]:
        user_turns = [item for item in batch if item.role == "user"]
        if not user_turns:
            return ["- None."]
        threads: list[str] = []
        seen: set[str] = set()
        for turn in user_turns[-3:]:
            line = self._shorten(turn.content, 120)
            if line.lower() in seen:
                continue
            seen.add(line.lower())
            threads.append(f"- {line}")
        return threads or ["- None."]

    def _ingest_vectors(
        self,
        session_key: str,
        history_entries: list[HistoryEntry],
        pending_items: list[PendingMemoryItem],
    ) -> None:
        texts = [item.text for item in history_entries] + [item.text for item in pending_items]
        if not texts:
            return
        embeddings = self._embedding_provider.embed_texts(texts)
        records: list[VectorMemoryRecord] = []
        offset = 0
        for entry in history_entries:
            records.append(
                VectorMemoryRecord(
                    record_id=f"history:{entry.source_ref}:{offset}",
                    source_ref=entry.source_ref,
                    session_key=session_key,
                    text=entry.text,
                    metadata={"kind": "history"},
                    embedding=embeddings[offset],
                    created_at=entry.created_at,
                )
            )
            offset += 1
        for item in pending_items:
            records.append(
                VectorMemoryRecord(
                    record_id=f"pending:{item.item_id}",
                    source_ref=item.source_ref,
                    session_key=session_key,
                    text=item.text,
                    metadata={"kind": "pending", "tag": item.tag},
                    embedding=embeddings[offset],
                    created_at=item.created_at,
                )
            )
            offset += 1
        self._vector_store.upsert_items(records)

    def _infer_tags(self, text: str) -> list[str]:
        lowered = text.lower()
        tags: list[str] = []
        if "remember" in lowered:
            tags.append("requested_memory")
        if any(phrase in lowered for phrase in ("i like", "i love", "i prefer", "my favorite", "i enjoy")):
            tags.append("preference")
        if any(phrase in lowered for phrase in ("i am", "i'm", "my name is", "i work as", "i live in")):
            tags.append("identity")
        if any(phrase in lowered for phrase in ("actually", "correction", "instead")):
            tags.append("correction")
        if any(phrase in lowered for phrase in ("i have", "i need", "i bought", "i started", "i'm learning")):
            tags.append("key_info")
        return tags

    def _shorten(self, text: str, limit: int) -> str:
        clean = " ".join(text.split())
        if len(clean) <= limit:
            return clean
        return clean[: limit - 3].rstrip() + "..."
