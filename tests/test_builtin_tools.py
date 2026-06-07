from pathlib import Path

import pytest

from app.tools.builtin import WorkspaceAccessError, build_builtin_tool_registry


def test_list_workspace_files_returns_entries_with_relative_paths(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    registry = build_builtin_tool_registry(workspace_root=tmp_path, memory_root=tmp_path / "memory")

    result = registry.get("list_workspace_files").handler({"path": "app"})  # type: ignore[union-attr]

    assert result == {
        "path": "app",
        "entries": [{"path": "app/main.py", "type": "file"}],
    }


def test_read_workspace_file_rejects_path_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    registry = build_builtin_tool_registry(workspace_root=tmp_path, memory_root=tmp_path / "memory")

    with pytest.raises(WorkspaceAccessError, match="escapes"):
        registry.get("read_workspace_file").handler({"path": "../outside.txt"})  # type: ignore[union-attr]


def test_read_memory_file_allows_only_known_files(tmp_path: Path) -> None:
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    (memory_root / "MEMORY.md").write_text("stable fact", encoding="utf-8")
    registry = build_builtin_tool_registry(workspace_root=tmp_path, memory_root=memory_root)

    allowed = registry.get("read_memory_file").handler({"name": "MEMORY.md"})  # type: ignore[union-attr]
    assert allowed == {"path": "MEMORY.md", "content": "stable fact"}

    with pytest.raises(WorkspaceAccessError, match="not allowed"):
        registry.get("read_memory_file").handler({"name": "notes.txt"})  # type: ignore[union-attr]
