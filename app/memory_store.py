from dataclasses import dataclass
from pathlib import Path


class MemoryStoreError(RuntimeError):
    """Raised when the memory workspace cannot be initialized or read."""


@dataclass(frozen=True)
class MemorySnapshot:
    memory_text: str
    recent_context_text: str
    pending_text: str
    history_text: str

    def has_content(self) -> bool:
        return any(
            section.strip()
            for section in (
                self.memory_text,
                self.recent_context_text,
                self.pending_text,
                self.history_text,
            )
        )


class MemoryStore:
    def __init__(self, *, root_dir: Path) -> None:
        self._root_dir = Path(root_dir)
        self._memory_path = self._root_dir / "MEMORY.md"
        self._recent_context_path = self._root_dir / "RECENT_CONTEXT.md"
        self._pending_path = self._root_dir / "PENDING.md"
        self._history_path = self._root_dir / "HISTORY.md"

    def ensure_initialized(self) -> None:
        try:
            self._root_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MemoryStoreError(
                f"Failed to create memory directory: {self._root_dir}"
            ) from exc

        for path in self._paths():
            if path.exists():
                continue
            try:
                path.write_text("", encoding="utf-8")
            except OSError as exc:
                raise MemoryStoreError(f"Failed to initialize memory file: {path}") from exc

    def load_snapshot(self) -> MemorySnapshot:
        return MemorySnapshot(
            memory_text=self.read_memory(),
            recent_context_text=self.read_recent_context(),
            pending_text=self.read_pending(),
            history_text=self.read_history(),
        )

    def read_memory(self) -> str:
        return self._read_text(self._memory_path)

    def read_recent_context(self) -> str:
        return self._read_text(self._recent_context_path)

    def read_pending(self) -> str:
        return self._read_text(self._pending_path)

    def read_history(self) -> str:
        return self._read_text(self._history_path)

    def write_memory(self, text: str) -> None:
        self._write_text(self._memory_path, text)

    def write_recent_context(self, text: str) -> None:
        self._write_text(self._recent_context_path, text)

    def write_pending(self, text: str) -> None:
        self._write_text(self._pending_path, text)

    def append_history(self, entry_text: str) -> None:
        existing = self.read_history()
        if existing and not existing.endswith("\n"):
            existing += "\n"
        self._write_text(self._history_path, f"{existing}{entry_text}")

    def _paths(self) -> tuple[Path, Path, Path, Path]:
        return (
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
