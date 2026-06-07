import json
from dataclasses import dataclass
from pathlib import Path


class MemoryStoreError(RuntimeError):
    """Raised when the memory workspace cannot be initialized or read."""


@dataclass(frozen=True)
class ConsolidationState:
    last_processed_history_line: int = 0
    pending_user_message_count: int = 0
    last_consolidated_at: str | None = None


@dataclass(frozen=True)
class MemorySnapshot:
    self_text: str
    memory_text: str
    recent_context_text: str
    pending_text: str
    history_text: str
    consolidation_state: ConsolidationState

    def has_content(self) -> bool:
        return any(
            section.strip()
            for section in (
                self.self_text,
                self.memory_text,
                self.recent_context_text,
                self.pending_text,
                self.history_text,
            )
        )


class MemoryStore:
    def __init__(self, *, root_dir: Path) -> None:
        self._root_dir = Path(root_dir)
        self._self_path = self._root_dir / "SELF.md"
        self._memory_path = self._root_dir / "MEMORY.md"
        self._recent_context_path = self._root_dir / "RECENT_CONTEXT.md"
        self._pending_path = self._root_dir / "PENDING.md"
        self._history_path = self._root_dir / "HISTORY.md"
        self._consolidation_state_path = self._root_dir / "consolidation_state.json"

    def ensure_initialized(self) -> None:
        try:
            self._root_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MemoryStoreError(
                f"Failed to create memory directory: {self._root_dir}"
            ) from exc

        for path in self._text_paths():
            if path.exists():
                continue
            try:
                path.write_text("", encoding="utf-8")
            except OSError as exc:
                raise MemoryStoreError(f"Failed to initialize memory file: {path}") from exc

        if not self._consolidation_state_path.exists():
            self.write_consolidation_state(ConsolidationState())

    def load_snapshot(self) -> MemorySnapshot:
        return MemorySnapshot(
            self_text=self.read_self(),
            memory_text=self.read_memory(),
            recent_context_text=self.read_recent_context(),
            pending_text=self.read_pending(),
            history_text=self.read_history(),
            consolidation_state=self.read_consolidation_state(),
        )

    def read_self(self) -> str:
        return self._read_text(self._self_path)

    def read_memory(self) -> str:
        return self._read_text(self._memory_path)

    def read_recent_context(self) -> str:
        return self._read_text(self._recent_context_path)

    def read_pending(self) -> str:
        return self._read_text(self._pending_path)

    def read_history(self) -> str:
        return self._read_text(self._history_path)

    def read_consolidation_state(self) -> ConsolidationState:
        try:
            raw = self._consolidation_state_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise MemoryStoreError(
                f"Failed to read memory file: {self._consolidation_state_path}"
            ) from exc

        if not raw:
            return ConsolidationState()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MemoryStoreError(
                f"Failed to parse consolidation state: {self._consolidation_state_path}"
            ) from exc

        try:
            last_processed_history_line = int(data["last_processed_history_line"])
            pending_user_message_count = int(data["pending_user_message_count"])
            last_consolidated_at = data["last_consolidated_at"]
        except (KeyError, TypeError, ValueError) as exc:
            raise MemoryStoreError(
                f"Failed to parse consolidation state: {self._consolidation_state_path}"
            ) from exc

        if last_processed_history_line < 0 or pending_user_message_count < 0:
            raise MemoryStoreError(
                f"Failed to parse consolidation state: {self._consolidation_state_path}"
            )
        if last_consolidated_at is not None and not isinstance(last_consolidated_at, str):
            raise MemoryStoreError(
                f"Failed to parse consolidation state: {self._consolidation_state_path}"
            )

        return ConsolidationState(
            last_processed_history_line=last_processed_history_line,
            pending_user_message_count=pending_user_message_count,
            last_consolidated_at=last_consolidated_at,
        )

    def write_self(self, text: str) -> None:
        self._write_text(self._self_path, text)

    def write_memory(self, text: str) -> None:
        self._write_text(self._memory_path, text)

    def write_recent_context(self, text: str) -> None:
        self._write_text(self._recent_context_path, text)

    def write_pending(self, text: str) -> None:
        self._write_text(self._pending_path, text)

    def write_consolidation_state(self, state: ConsolidationState) -> None:
        payload = {
            "last_processed_history_line": state.last_processed_history_line,
            "pending_user_message_count": state.pending_user_message_count,
            "last_consolidated_at": state.last_consolidated_at,
        }
        try:
            self._consolidation_state_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise MemoryStoreError(
                f"Failed to write memory file: {self._consolidation_state_path}"
            ) from exc

    def append_history(self, entry_text: str) -> None:
        existing = self.read_history()
        if existing and not existing.endswith("\n"):
            existing += "\n"
        self._write_text(self._history_path, f"{existing}{entry_text}")

    def _text_paths(self) -> tuple[Path, Path, Path, Path, Path]:
        return (
            self._self_path,
            self._memory_path,
            self._recent_context_path,
            self._pending_path,
            self._history_path,
        )

    def _read_text(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise MemoryStoreError(f"Failed to read memory file: {path}") from exc

    def _write_text(self, path: Path, text: str) -> None:
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise MemoryStoreError(f"Failed to write memory file: {path}") from exc
