from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Protocol

from jarvis.memory.models import MemoryRecallHit, VectorMemoryRecord


class VectorStore(Protocol):
    def upsert_items(self, items: list[VectorMemoryRecord]) -> None: ...

    def search(
        self,
        query_embedding: list[float],
        *,
        top_k: int,
        metadata_filters: dict[str, Any] | None = None,
    ) -> list[MemoryRecallHit]: ...

    def delete_items(self, record_ids: list[str]) -> None: ...


class FileVectorStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._write([])

    def upsert_items(self, items: list[VectorMemoryRecord]) -> None:
        if not items:
            return
        current = {item.record_id: item for item in self._read()}
        for item in items:
            current[item.record_id] = item
        self._write(list(current.values()))

    def search(
        self,
        query_embedding: list[float],
        *,
        top_k: int,
        metadata_filters: dict[str, Any] | None = None,
    ) -> list[MemoryRecallHit]:
        if not query_embedding or not any(query_embedding):
            return []
        results: list[MemoryRecallHit] = []
        for item in self._read():
            if metadata_filters and not self._matches_filters(item, metadata_filters):
                continue
            score = self._cosine_similarity(query_embedding, item.embedding)
            if score <= 0:
                continue
            results.append(
                MemoryRecallHit(
                    text=item.text,
                    source_ref=item.source_ref,
                    score=score,
                    metadata=item.metadata,
                )
            )
        results.sort(key=lambda item: item.score, reverse=True)
        return results[:top_k]

    def delete_items(self, record_ids: list[str]) -> None:
        if not record_ids:
            return
        ids = set(record_ids)
        self._write([item for item in self._read() if item.record_id not in ids])

    def _read(self) -> list[VectorMemoryRecord]:
        return [VectorMemoryRecord.model_validate(item) for item in json.loads(self._path.read_text(encoding="utf-8"))]

    def _write(self, items: list[VectorMemoryRecord]) -> None:
        self._path.write_text(
            json.dumps([item.model_dump(mode="json") for item in items], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _matches_filters(self, item: VectorMemoryRecord, metadata_filters: dict[str, Any]) -> bool:
        for key, expected in metadata_filters.items():
            if key == "session_key" and item.session_key != expected:
                return False
            if key != "session_key" and item.metadata.get(key) != expected:
                return False
        return True

    def _cosine_similarity(self, a: list[float], b: list[float]) -> float:
        if not a or not b:
            return 0.0
        numerator = sum(left * right for left, right in zip(a, b))
        denom_a = math.sqrt(sum(item * item for item in a))
        denom_b = math.sqrt(sum(item * item for item in b))
        if denom_a == 0 or denom_b == 0:
            return 0.0
        return numerator / (denom_a * denom_b)
