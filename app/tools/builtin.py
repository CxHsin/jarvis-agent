from pathlib import Path

from app.tools.base import ToolSpec
from app.tools.registry import ToolRegistry


class WorkspaceAccessError(RuntimeError):
    """Raised when a tool attempts to access a disallowed path."""


def build_builtin_tool_registry(*, workspace_root: Path, memory_root: Path) -> ToolRegistry:
    return ToolRegistry(
        [
            ToolSpec(
                name="list_workspace_files",
                description="List files and directories inside the workspace.",
                arguments=("path",),
                handler=_build_workspace_lister(workspace_root),
            ),
            ToolSpec(
                name="read_workspace_file",
                description="Read a UTF-8 text file inside the workspace.",
                arguments=("path",),
                handler=_build_workspace_reader(workspace_root),
            ),
            ToolSpec(
                name="read_memory_file",
                description="Read one of the managed memory markdown files.",
                arguments=("name",),
                handler=_build_memory_reader(memory_root),
            ),
        ]
    )


def _build_workspace_lister(workspace_root: Path):
    root = workspace_root.resolve()

    def handler(arguments: dict[str, str]) -> dict[str, object]:
        target = _resolve_inside_root(root=root, relative_path=arguments["path"])
        entries = []
        for path in sorted(target.iterdir(), key=lambda item: item.name.casefold()):
            entry_type = "dir" if path.is_dir() else "file"
            entries.append({"path": path.relative_to(root).as_posix(), "type": entry_type})
        return {"path": target.relative_to(root).as_posix() or ".", "entries": entries}

    return handler


def _build_workspace_reader(workspace_root: Path):
    root = workspace_root.resolve()

    def handler(arguments: dict[str, str]) -> dict[str, object]:
        target = _resolve_inside_root(root=root, relative_path=arguments["path"])
        if not target.is_file():
            raise WorkspaceAccessError("Target is not a file.")
        return {"path": target.relative_to(root).as_posix(), "content": target.read_text(encoding="utf-8")}

    return handler


def _build_memory_reader(memory_root: Path):
    root = memory_root.resolve()
    allowed = frozenset(
        {"SELF.md", "MEMORY.md", "RECENT_CONTEXT.md", "PENDING.md", "HISTORY.md"}
    )

    def handler(arguments: dict[str, str]) -> dict[str, object]:
        name = arguments["name"]
        if name not in allowed:
            raise WorkspaceAccessError("Memory file is not allowed.")
        target = _resolve_inside_root(root=root, relative_path=name)
        return {"path": target.relative_to(root).as_posix(), "content": target.read_text(encoding="utf-8")}

    return handler


def _resolve_inside_root(*, root: Path, relative_path: str) -> Path:
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise WorkspaceAccessError("Path escapes the allowed root.") from exc
    return candidate
