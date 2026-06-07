from pathlib import Path

import pytest

from app.memory_store import ConsolidationState, MemorySnapshot, MemoryStore, MemoryStoreError


def test_memory_store_initializes_missing_directory_and_files(tmp_path: Path) -> None:
    store = MemoryStore(root_dir=tmp_path / "memory")

    store.ensure_initialized()

    for name in (
        "SELF.md",
        "MEMORY.md",
        "RECENT_CONTEXT.md",
        "PENDING.md",
        "HISTORY.md",
        "consolidation_state.json",
    ):
        assert (tmp_path / "memory" / name).exists()


def test_memory_store_loads_all_files_into_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    store = MemoryStore(root_dir=root)
    store.ensure_initialized()
    (root / "SELF.md").write_text("self", encoding="utf-8")
    (root / "MEMORY.md").write_text("long term", encoding="utf-8")
    (root / "RECENT_CONTEXT.md").write_text("recent", encoding="utf-8")
    (root / "PENDING.md").write_text("pending", encoding="utf-8")
    (root / "HISTORY.md").write_text("history", encoding="utf-8")
    store.write_consolidation_state(
        ConsolidationState(
            last_processed_history_line=4,
            pending_user_message_count=2,
            last_consolidated_at="2026-06-07T00:00:00+00:00",
        )
    )

    snapshot = store.load_snapshot()

    assert snapshot == MemorySnapshot(
        self_text="self",
        memory_text="long term",
        recent_context_text="recent",
        pending_text="pending",
        history_text="history",
        consolidation_state=ConsolidationState(
            last_processed_history_line=4,
            pending_user_message_count=2,
            last_consolidated_at="2026-06-07T00:00:00+00:00",
        ),
    )


def test_memory_store_recreates_missing_file_on_reinitialize(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    store = MemoryStore(root_dir=root)
    store.ensure_initialized()
    (root / "PENDING.md").unlink()

    store.ensure_initialized()

    assert (root / "PENDING.md").exists()


def test_memory_store_append_history_appends_newline_when_needed(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    store = MemoryStore(root_dir=root)
    store.ensure_initialized()
    store.write_memory("ignored")
    (root / "HISTORY.md").write_text("first", encoding="utf-8")

    store.append_history("second")

    assert store.read_history() == "first\nsecond"


def test_memory_store_reads_blank_consolidation_state_as_default(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    store = MemoryStore(root_dir=root)
    store.ensure_initialized()
    (root / "consolidation_state.json").write_text("", encoding="utf-8")

    assert store.read_consolidation_state() == ConsolidationState()


def test_memory_store_raises_when_root_is_a_file(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    root.write_text("not a dir", encoding="utf-8")
    store = MemoryStore(root_dir=root)

    with pytest.raises(MemoryStoreError, match="create memory directory"):
        store.ensure_initialized()
