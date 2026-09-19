"""Workspace file operations and shell tool handlers."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, TYPE_CHECKING

from context.context_budget import estimate_tokens
from tools.tool_runtime import failure

if TYPE_CHECKING:
    from configuration import Config

def decode_shell_output(data: bytes) -> str:
    """Accept UTF-8 programs and the Windows shell's native OEM output."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("oem" if os.name == "nt" else "utf-8", errors="replace")

class WorkspaceError(ValueError):
    """Raised for invalid or unsupported workspace operations."""

def _display_path(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return str(path)
    return "." if str(relative) == "." else relative.as_posix()


class Workspace:
    """File tools constrained to one configured root directory."""

    def __init__(self, config: Config):
        self.config = config
        self.root = config.root_dir.resolve()

    def _resolve(self, user_path: str | None) -> Path:
        raw = Path(user_path or ".")
        candidate = (raw if raw.is_absolute() else self.root / raw).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError("路径超出允许的工作区范围。") from exc
        return candidate

    def _resolve_read(self, user_path: str | None) -> Path:
        """Resolve a read target without restricting it to the workspace root."""
        raw = Path(user_path or ".").expanduser()
        return (raw if raw.is_absolute() else self.root / raw).resolve()

    def list_directory(self, path: str = ".") -> dict[str, Any]:
        directory = self._resolve(path)
        if not directory.exists():
            raise WorkspaceError(f"目录不存在: {_display_path(directory, self.root)}")
        if not directory.is_dir():
            raise WorkspaceError(f"不是目录: {_display_path(directory, self.root)}")
        entries = []
        for entry in sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            try:
                resolved = entry.resolve()
                resolved.relative_to(self.root)
            except (OSError, ValueError):
                continue
            entries.append(
                {
                    "name": entry.name,
                    "type": "directory" if entry.is_dir() else "file",
                    "path": _display_path(resolved, self.root),
                }
            )
        limit = int(getattr(self.config, "max_directory_entries", 0) or 0)
        truncated = bool(limit) and len(entries) > limit
        if truncated:
            entries = entries[:limit]
        return self._fit_result(
            "list_directory",
            {"ok": True, "path": _display_path(directory, self.root), "entries": entries, "truncated": truncated},
        )

    def _is_text_file(self, path: Path) -> bool:
        return path.suffix.lower() in self.config.text_extensions

    @staticmethod
    def _result_tokens(result: Mapping[str, Any]) -> int:
        """Tokens the model spends on this result once it becomes a tool message."""

        return estimate_tokens([{"role": "tool", "content": json.dumps(result, ensure_ascii=False)}], [])

    @staticmethod
    def _leading_line_number(entry: str) -> int | None:
        head, separator, _ = entry.partition(":")
        return int(head) if separator and head.isdigit() else None

    def _fit_result(self, name: str, result: dict[str, Any]) -> dict[str, Any]:
        """Trim a tool result to the token budget before the model sees it.

        Character caps stay as a cheap first filter; this guard bounds what a
        token-dense result costs in the request and marks every trimmed result
        so the model knows it is looking at a partial answer.
        """

        limit = int(getattr(self.config, "max_tool_result_tokens", 0) or 0)
        if limit <= 0 or self._result_tokens(result) <= limit:
            return result
        if name == "read_file" and isinstance(result.get("content"), str):
            return self._trim_read_result(result, limit)
        for field, tool in (("matches", "search_file_content"), ("entries", "list_directory")):
            if name == tool and isinstance(result.get(field), list):
                return self._trim_list_result(result, field, limit)
        preview = json.dumps(result, ensure_ascii=False)[:200]
        return {
            "ok": bool(result.get("ok")),
            "truncated": True,
            "path": result.get("path"),
            "error": result.get("error"),
            "preview": preview,
        }

    def _trim_read_result(self, result: dict[str, Any], limit: int) -> dict[str, Any]:
        lines = str(result.get("content", "")).split("\n")
        marker = "[内容已按上下文预算截断]"
        low, high = 0, len(lines)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = {**result, "content": "\n".join(lines[:middle]) + "\n" + marker, "truncated": True}
            last_line = self._leading_line_number(lines[middle - 1])
            if last_line is not None:
                candidate.update(end_line=last_line, next_start_line=last_line + 1)
            if self._result_tokens(candidate) <= limit:
                low = middle
            else:
                high = middle - 1
        kept = lines[:low]
        last = next((number for entry in reversed(kept)
                     if (number := self._leading_line_number(entry)) is not None), None)
        if last is None:
            return {**result, "content": marker, "truncated": True,
                    "end_line": result.get("start_line", 1) - 1}
        return {**result, "content": "\n".join(kept) + "\n" + marker, "truncated": True,
                "end_line": last, "next_start_line": last + 1}

    def _trim_list_result(self, result: dict[str, Any], field: str, limit: int) -> dict[str, Any]:
        items = list(result[field])
        low, high = 0, len(items)
        while low < high:
            middle = (low + high + 1) // 2
            if self._result_tokens({**result, field: items[:middle], "truncated": True}) <= limit:
                low = middle
            else:
                high = middle - 1
        return {**result, field: items[:low], "truncated": True}

    def _read_text(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            raise WorkspaceError(f"读取文件失败: {exc}") from exc

    def search_file_content(self, query: str, path: str = ".") -> dict[str, Any]:
        if not query or not query.strip():
            raise WorkspaceError("query 不能为空。")
        start = self._resolve(path)
        if not start.exists():
            raise WorkspaceError(f"路径不存在: {_display_path(start, self.root)}")
        candidates = [start] if start.is_file() else list(start.rglob("*"))
        matches: list[dict[str, Any]] = []
        needle = query.casefold()
        for candidate in sorted(candidates, key=lambda item: str(item).lower()):
            if len(matches) >= self.config.max_search_matches:
                break
            if not candidate.is_file() or not self._is_text_file(candidate):
                continue
            try:
                candidate.resolve().relative_to(self.root)
            except ValueError:
                continue
            text = self._read_text(candidate)
            for line_number, line in enumerate(text.splitlines(), 1):
                if needle in line.casefold():
                    matches.append(
                        {
                            "path": _display_path(candidate.resolve(), self.root),
                            "line": line_number,
                            "snippet": line[:500],
                        }
                    )
                    if len(matches) >= self.config.max_search_matches:
                        break
        return self._fit_result(
            "search_file_content",
            {"ok": True, "query": query, "matches": matches,
             "truncated": len(matches) >= self.config.max_search_matches},
        )

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
        file_path = self._resolve_read(path)
        if not file_path.exists():
            raise WorkspaceError(f"文件不存在: {_display_path(file_path, self.root)}")
        if not file_path.is_file():
            raise WorkspaceError(f"不是文件: {_display_path(file_path, self.root)}")
        if not self._is_text_file(file_path):
            raise WorkspaceError(f"第一阶段只支持文本文件: {file_path.suffix or '(无扩展名)'}")
        if start_line < 1 or (end_line is not None and end_line < start_line):
            raise WorkspaceError("行号范围无效。")
        try:
            file_bytes = file_path.read_bytes()
        except OSError as exc:
            raise WorkspaceError(f"读取文件失败: {exc}") from exc
        lines = file_bytes.decode("utf-8-sig", errors="replace").splitlines()
        selected_end = min(end_line or len(lines), len(lines))
        numbered: list[str] = []
        used = 0
        last_line = start_line - 1
        truncated = False
        for number, line in enumerate(lines[start_line - 1 : selected_end], start_line):
            entry = f"{number}: {line}"
            addition = len(entry) + (1 if numbered else 0)
            if used + addition > self.config.max_read_chars:
                if not numbered:
                    numbered.append(entry[: self.config.max_read_chars])
                    last_line = number
                truncated = True
                break
            numbered.append(entry)
            used += addition
            last_line = number
        result: dict[str, Any] = {
            "ok": True,
            "path": _display_path(file_path, self.root),
            "start_line": start_line,
            "end_line": last_line,
            "content": "\n".join(numbered) if numbered else "[内容为空]",
            "truncated": truncated,
            "hash": hashlib.sha256(file_bytes).hexdigest(),
        }
        if truncated:
            result["next_start_line"] = last_line + 1
        return self._fit_result("read_file", result)

    def read(self, path: str, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
        return self.read_file(path, start_line, end_line)

    def edit(self, path: str, content: str, *, start_line: int | None = None, end_line: int | None = None,
             expected_hash: str | None = None, execution_context=None) -> dict[str, Any]:
        target = self._resolve(path)
        from session.session_store import resolve_state_dir
        protected_roots = (resolve_state_dir(self.config).resolve(), Path(__file__).resolve().parents[1],
                           Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve())
        if any(target == root or target.is_relative_to(root) for root in protected_roots):
            raise WorkspaceError("Agent 状态和运行时代码不可由文件工具修改；记忆只能通过 memory_manage 修改。")
        current_hash = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else "missing"
        if expected_hash is not None and expected_hash != current_hash:
            return failure("edit_conflict", "文件已改变，请重新读取后再编辑。", current_hash=current_hash)
        if target.exists() and not target.is_file(): raise WorkspaceError("不是文件。")
        if target.exists() and not self._is_text_file(target): raise WorkspaceError("只支持文本文件编辑。")
        if start_line is None and end_line is not None: raise WorkspaceError("end_line 需要 start_line。")
        if start_line is None: updated = str(content)
        else:
            if start_line < 1 or (end_line is not None and end_line < start_line): raise WorkspaceError("行号范围无效。")
            lines = self._read_text(target).splitlines() if target.exists() else []
            if start_line > len(lines) + 1: raise WorkspaceError("start_line 超出文件范围。")
            finish = min(end_line or start_line, len(lines)); lines[start_line - 1:finish] = str(content).splitlines(); updated = "\n".join(lines) + ("\n" if lines else "")
        def commit():
            # Recheck after preparing the edit, then atomically replace the file.
            import tempfile
            latest_hash = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else "missing"
            if latest_hash != current_hash:
                return failure("edit_conflict", "文件在编辑期间改变。")
            target.parent.mkdir(parents=True, exist_ok=True)
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                    temp_path = Path(handle.name)
                    handle.write(updated.encode("utf-8"))
                os.replace(temp_path, target)
            finally:
                if temp_path is not None and temp_path.exists():
                    temp_path.unlink()
            return {"ok": True, "path": _display_path(target, self.root), "bytes": len(updated.encode("utf-8")),
                    "hash": hashlib.sha256(updated.encode("utf-8")).hexdigest()}
        try:
            return execution_context.commit(commit) if execution_context else commit()
        except OSError as exc:
            raise WorkspaceError(f"写入文件失败: {exc}") from exc

    def bash(self, command: str, timeout: float = 10.0, execution_context=None) -> dict[str, Any]:
        from tools.shell_sandbox import start_shell
        from session.session_store import resolve_state_dir
        import tempfile
        import time
        if not command or not command.strip(): raise WorkspaceError("command 不能为空。")
        with tempfile.TemporaryFile() as output_file:
            def start():
                return start_shell(command, self.root, resolve_state_dir(self.config), output_file,
                                   execution_context.check if execution_context else lambda: None)
            process = start()
            try:
                deadline = time.monotonic() + min(float(timeout), 60.0)
                if execution_context:
                    execution_context.commit(process.start)
                else:
                    process.start()
                while process.poll() is None:
                    if execution_context:
                        execution_context.check()
                    if time.monotonic() >= deadline:
                        raise TimeoutError("命令执行超时。")
                    time.sleep(0.01)
            finally:
                process.close()
            output_file.seek(0)
            limit = max(1000, self.config.max_read_chars)
            data = output_file.read(limit + 1)
        return {"ok": process.returncode == 0, "exit_code": process.returncode,
                "output": decode_shell_output(data[:limit]), "truncated": len(data) > limit}
