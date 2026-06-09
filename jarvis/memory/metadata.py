from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class FileMemoryMetadataStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._write(
                {
                    "consolidation": {
                        "last_sequence_by_session": {},
                        "processed_source_refs": [],
                    },
                    "optimizer": {"last_run_at": None},
                }
            )

    def get_last_consolidated_sequence(self, session_key: str) -> int:
        data = self._read()
        return int(data["consolidation"]["last_sequence_by_session"].get(session_key, 0))

    def set_last_consolidated_sequence(self, session_key: str, sequence: int) -> None:
        data = self._read()
        data["consolidation"]["last_sequence_by_session"][session_key] = sequence
        self._write(data)

    def has_processed_source_ref(self, source_ref: str) -> bool:
        data = self._read()
        return source_ref in data["consolidation"]["processed_source_refs"]

    def mark_processed_source_ref(self, source_ref: str) -> None:
        data = self._read()
        refs = data["consolidation"]["processed_source_refs"]
        if source_ref not in refs:
            refs.append(source_ref)
            self._write(data)

    def get_last_optimizer_run_at(self) -> datetime | None:
        data = self._read()
        raw = data["optimizer"].get("last_run_at")
        if not raw:
            return None
        return datetime.fromisoformat(str(raw))

    def set_last_optimizer_run_at(self, occurred_at: datetime) -> None:
        data = self._read()
        data["optimizer"]["last_run_at"] = occurred_at.isoformat()
        self._write(data)

    def _read(self) -> dict[str, object]:
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, object]) -> None:
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
