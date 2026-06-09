from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from jarvis.config import AppConfig
from jarvis.memory.context import ContextStore, JsonContextStore
from jarvis.memory.consolidation import MemoryConsolidator
from jarvis.memory.embeddings import EmbeddingProvider, OpenAICompatibleEmbeddingProvider
from jarvis.memory.markdown_store import MarkdownMemoryStore
from jarvis.memory.metadata import FileMemoryMetadataStore
from jarvis.memory.models import ContextTurn, PendingMemoryItem, RetrievedMemory, TurnCommitted
from jarvis.memory.optimizer import MemoryOptimizer
from jarvis.memory.vector_store import FileVectorStore, VectorStore


class MemoryRetrievalApi(Protocol):
    def retrieve_for_turn(self, session_key: str, query_text: str) -> RetrievedMemory: ...

    def retrieve_explicit(self, session_key: str, query_text: str) -> RetrievedMemory: ...

    def retrieve_for_proactive(self, session_key: str) -> RetrievedMemory: ...


class MemoryWriteApi(Protocol):
    def remember(self, session_key: str, text: str, *, tag: str = "requested_memory") -> None: ...

    def forget(self, session_key: str, text: str) -> None: ...

    def correct(self, session_key: str, text: str) -> None: ...


class MemoryMaintenanceApi(Protocol):
    def on_turn_committed(self, turn: TurnCommitted) -> bool: ...

    def should_consolidate(self, session_key: str) -> bool: ...

    def consolidate(self, session_key: str) -> bool: ...

    def optimize(self, occurred_at: datetime) -> tuple[bool, str]: ...


@dataclass(frozen=True)
class PromptContextAssembler:
    memory: "MemoryEngine"
    system_prompt: str

    def build_messages(self, session_key: str, query_text: str) -> list[dict[str, Any]]:
        retrieved = self.memory.retrieve_for_turn(session_key, query_text)
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        if retrieved.self_text.strip():
            messages.append({"role": "system", "content": retrieved.self_text})
        if retrieved.memory_text.strip():
            messages.append({"role": "system", "content": retrieved.memory_text})
        if retrieved.recent_context_text.strip():
            messages.append({"role": "system", "content": retrieved.recent_context_text})
        vector_block = self._format_vector_block(retrieved)
        if vector_block:
            messages.append({"role": "system", "content": vector_block})
        for item in retrieved.recent_turns:
            messages.append({"role": item.role, "content": item.content})
        return messages

    def _format_vector_block(self, retrieved: RetrievedMemory) -> str:
        if not retrieved.vector_hits:
            return ""
        lines = ["# Semantic Recall", ""]
        for hit in retrieved.vector_hits:
            lines.append(f"- ({hit.score:.2f}) {hit.text}")
        lines.append("")
        return "\n".join(lines)


class MemoryEngine(MemoryRetrievalApi, MemoryWriteApi, MemoryMaintenanceApi):
    def __init__(
        self,
        *,
        context_store: ContextStore,
        markdown_store: MarkdownMemoryStore,
        consolidator: MemoryConsolidator,
        optimizer: MemoryOptimizer,
        embedding_provider: EmbeddingProvider,
        vector_store: VectorStore,
        enabled: bool,
        vector_recall_limit: int,
    ) -> None:
        self._context_store = context_store
        self._markdown_store = markdown_store
        self._consolidator = consolidator
        self._optimizer = optimizer
        self._embedding_provider = embedding_provider
        self._vector_store = vector_store
        self._enabled = enabled
        self._vector_recall_limit = max(1, vector_recall_limit)

    @property
    def context_store(self) -> ContextStore:
        return self._context_store

    def append_turn(self, session_key: str, role: str, content: str) -> ContextTurn:
        return self._context_store.append_turn(session_key, role, content)

    def retrieve_for_turn(self, session_key: str, query_text: str) -> RetrievedMemory:
        recent_turns = self._context_store.get_recent_turns(session_key)
        if not self._enabled:
            return RetrievedMemory(recent_turns=recent_turns)
        vector_hits = []
        if query_text.strip():
            try:
                embedding = self._embedding_provider.embed_texts([query_text])[0]
            except Exception:
                embedding = []
            if embedding:
                try:
                    vector_hits = self._vector_store.search(
                        embedding,
                        top_k=self._vector_recall_limit,
                        metadata_filters={"session_key": session_key},
                    )
                except Exception:
                    vector_hits = []
        return RetrievedMemory(
            self_text=self._markdown_store.read_self(),
            memory_text=self._markdown_store.read_memory(),
            recent_context_text=self._markdown_store.read_recent_context(),
            vector_hits=vector_hits,
            recent_turns=recent_turns,
        )

    def retrieve_explicit(self, session_key: str, query_text: str) -> RetrievedMemory:
        return self.retrieve_for_turn(session_key, query_text)

    def retrieve_for_proactive(self, session_key: str) -> RetrievedMemory:
        return self.retrieve_for_turn(session_key, "")

    def remember(self, session_key: str, text: str, *, tag: str = "requested_memory") -> None:
        item = PendingMemoryItem(item_id=f"{session_key}:manual:{tag}", source_ref=f"manual:{session_key}", tag=tag, text=text)
        self._markdown_store.append_pending_items([item])

    def forget(self, session_key: str, text: str) -> None:
        self.correct(session_key, f"Forget or remove: {text}")

    def correct(self, session_key: str, text: str) -> None:
        item = PendingMemoryItem(item_id=f"{session_key}:manual:correction", source_ref=f"manual:{session_key}", tag="correction", text=text)
        self._markdown_store.append_pending_items([item])

    def on_turn_committed(self, turn: TurnCommitted) -> bool:
        if not self._enabled:
            return False
        return self._consolidator.on_turn_committed(turn)

    def should_consolidate(self, session_key: str) -> bool:
        return self._consolidator.should_consolidate(session_key)

    def consolidate(self, session_key: str) -> bool:
        return self._consolidator.consolidate(session_key)

    def optimize(self, occurred_at: datetime) -> tuple[bool, str]:
        return self._optimizer.optimize(occurred_at)


def build_memory_engine(
    config: AppConfig,
    *,
    context_store: JsonContextStore | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> MemoryEngine:
    store = context_store or JsonContextStore(config.context_store_path, keep_count=config.runtime.session_history_limit)
    markdown_store = MarkdownMemoryStore(config.memory_dir_path)
    metadata_store = FileMemoryMetadataStore(config.memory_dir_path / "metadata.json")
    resolved_embedding_provider = embedding_provider or OpenAICompatibleEmbeddingProvider(config.llm)
    vector_store = FileVectorStore(config.memory_dir_path / "vectors.json")
    consolidator = MemoryConsolidator(
        context_store=store,
        markdown_store=markdown_store,
        metadata_store=metadata_store,
        embedding_provider=resolved_embedding_provider,
        vector_store=vector_store,
        keep_count=config.runtime.session_history_limit,
    )
    optimizer = MemoryOptimizer(
        markdown_store=markdown_store,
        metadata_store=metadata_store,
        enabled=config.runtime.memory_optimizer_enabled,
        interval_seconds=config.runtime.memory_optimizer_interval_seconds,
    )
    return MemoryEngine(
        context_store=store,
        markdown_store=markdown_store,
        consolidator=consolidator,
        optimizer=optimizer,
        embedding_provider=resolved_embedding_provider,
        vector_store=vector_store,
        enabled=config.runtime.memory_enabled,
        vector_recall_limit=config.runtime.memory_vector_recall_limit,
    )
