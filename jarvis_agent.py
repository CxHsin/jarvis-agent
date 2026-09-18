"""Stage-one command-line personal agent.

The implementation deliberately keeps the agent loop visible: a model response
may request tools, independent calls can run concurrently, and the results are
returned to the model until it answers or the round limit is reached.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from context_manager import CONTEXT_RECOVERED_MARKER, ContextManager, estimate_tokens
from cache_metrics import MeasuredClient, UsageLedger
from compaction import CompactionResult, MANUAL, OVERFLOW, is_overflow_error
from context_budget import ContextBudget
from memory_profile import ProfileEditError
from session_store import (SessionContents, SessionLockedError, SessionNotFoundError, SessionStore,
                           resolve_state_dir)
from tool_runtime import (ToolRegistry, ToolRuntime,
                          PermissionPolicy, ProviderSession, ToolProviderAdapter, ProviderLoadError, failure)


DEFAULT_TEXT_EXTENSIONS = (".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".py")


def decode_shell_output(data: bytes) -> str:
    """Accept UTF-8 programs and the Windows shell's native OEM output."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("oem" if os.name == "nt" else "utf-8", errors="replace")


from configuration import (Config, ConfigurationError, DEFAULT_SYSTEM_PROMPT,
                           load_global_tool_permission_mode, save_global_tool_permission_mode)
from model_client import ChatCompletionsClient, ModelRequestError
from application import Application


class WorkspaceError(ValueError):
    """Raised for invalid or unsupported workspace operations."""


def parse_compact_argument(argument: str) -> int | None:
    """Return the optional keep target for the /compact command."""

    text = argument.strip()
    if not text:
        return None
    if not text.isdigit() or int(text) < 1:
        raise ValueError("用法：/compact [保留的 token 数]，例如 /compact 2000；不带参数时保留当前任务的原文。")
    return int(text)


PERMISSION_COMMANDS = {
    "/all": "approve-all",
    "/safe": "approve-dangerous",
    "/wide": "broad-access",
}


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
        from session_store import resolve_state_dir
        protected_roots = (resolve_state_dir(self.config).resolve(), Path(__file__).resolve().parent,
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
        from shell_sandbox import start_shell
        from session_store import resolve_state_dir
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


TOOL_DEFINITIONS: list[dict[str, Any]] = [
 {"type":"function","function":{"name":"read","description":"读取文本文件；支持工作区外的绝对路径，相对路径以工作区为基准。","parameters":{"type":"object","properties":{"path":{"type":"string"},"start_line":{"type":"integer","minimum":1},"end_line":{"type":"integer","minimum":1}},"required":["path"],"additionalProperties":False}}},
 {"type":"function","function":{"name":"edit","description":"编辑工作区文本文件。","parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"},"start_line":{"type":"integer","minimum":1},"end_line":{"type":"integer","minimum":1}},"required":["path","content"],"additionalProperties":False}}},
 {"type":"function","function":{"name":"bash","description":"在 Windows AppContainer 沙箱中通过 cmd.exe 执行命令，并非 Bash 或 PowerShell。不要使用 ls/head 等 Unix 命令。列目录使用 list_directory；cmd 的 dir 在沙箱中可能拒绝访问。读取文件使用 read。","parameters":{"type":"object","properties":{"command":{"type":"string"},"timeout":{"type":"number","minimum":0.1,"maximum":60}},"required":["command"],"additionalProperties":False}}},
 {"type":"function","function":{"name":"tool_search","description":"搜索可用工具。","parameters":{"type":"object","properties":{"query":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":20}},"additionalProperties":False}}},
]
TOOL_DEFINITIONS += [
 {"type":"function","function":{"name":"list_directory","description":"列出工作区目录的直接子项，返回文件和子目录路径及截断标记。查看子目录时再次调用；列目录优先使用本工具，无需 shell。","parameters":{"type":"object","properties":{"path":{"type":"string","description":"工作区内的目录路径，默认当前工作区。"}},"additionalProperties":False}}},
 {"type":"function","function":{"name":"memory_search","description":"Search personal memory facts.","parameters":{"type":"object","properties":{"query":{"type":"string"},"include_history":{"type":"boolean"}},"required":["query"],"additionalProperties":False}}},
 {"type":"function","function":{"name":"memory_manage","description":"Remember, correct, or forget a personal fact.","parameters":{"type":"object","properties":{"action":{"type":"string","enum":["remember","correct","forget"]},"fact_id":{"type":"string"},"fact":{"type":"object"}},"required":["action"],"additionalProperties":False}}}
]

TOOL_DEFINITIONS[1]["function"]["parameters"]["properties"]["expected_hash"] = {
    "type": "string", "description": "先前读取的 SHA-256；新文件使用 missing。"}
for _definition in TOOL_DEFINITIONS:
    _properties = _definition["function"]["parameters"]["properties"]
    _properties["_depends_on"] = {"type": "array", "items": {"type": "string"},
                                  "description": "同一批次内必须先成功的 tool_call IDs。"}
    _properties["_version"] = {"type": "string"}
    _properties["_schema_fingerprint"] = {"type": "string"}



ToolFunction = Callable[..., dict[str, Any]]


class Agent:
    def __init__(
        self,
        config: Config,
        client: ChatCompletionsClient | Any | None = None,
        compression_client: ChatCompletionsClient | Any | None = None,
        store: SessionStore | None = None,
        resume: str | None = None,
        tool_runtime: ToolRuntime | None = None,
        permission_policy: PermissionPolicy | None = None,
        confirm_tool: Callable | None = None,
        native_loader: Callable | None = None,
        extraction_client: ChatCompletionsClient | Any | None = None,
        embedding_client: Any | None = None,
        rewrite_client: Any | None = None,
        memory_authorization_client: Any | None = None,
        application: Application | None = None,
    ):
        supplied_runtime = tool_runtime is not None
        self._owns_application = application is None
        self.application = application or Application(config, client=client,
            compression_client=compression_client, extraction_client=extraction_client,
            embedding_client=embedding_client, rewrite_client=rewrite_client,
            memory_authorization_client=memory_authorization_client)
        self.store = store
        try:
            if self.store is None and resume is not None:
                # Acquire the session lock before model discovery or background work.
                self.store = SessionStore.resume(config, resume or None, memory=self.application.memory)
            self.application.prepare(self.store.memory if self.store is not None else None)
            self.config = self.application.config
            if self.store is None:
                self.store = SessionStore.create(self.config, memory=self.application.memory)
            elif self.store.memory is not self.application.memory:
                self.store.memory = self.application.memory
                self.store.history.memory = self.application.memory
        except BaseException:
            if self.store is not None:
                self.store.close()
            if self._owns_application:
                self.application.close()
            raise
        try:
            self.workspace = Workspace(self.config)
            self.memory_authorization_client = self.application.memory_authorization_client
            self.tool_registry = tool_runtime.registry if tool_runtime is not None else ToolRegistry()
            self.usage_ledger = UsageLedger()
            self.compression_client = MeasuredClient(self.application.compression_client, self.usage_ledger,
                                                    "压缩模型", config.compression_model or config.model)
            self.client = MeasuredClient(self.application.client, self.usage_ledger, "主模型", config.model)
            self._audit_events = []
            self.messages: list[dict[str, Any]] = [{"role": "system", "content":
                self.store.memory.prefix_snapshot() + "\n\n" + self._system_prompt()}]
            self.context = ContextManager(self.config, recorder=self.store)
            self.tool_functions: dict[str, ToolFunction] = {
                "read": self.workspace.read, "edit": self.workspace.edit, "bash": self.workspace.bash,
                "tool_search": self._search_tools,
                "list_directory": self.workspace.list_directory, "search_file_content": self.workspace.search_file_content, "read_file": self.workspace.read_file,
            }
            self.tool_runtime = tool_runtime or ToolRuntime(self.tool_registry,
                stable=tuple((name, "1") for name in
                             ("read", "edit", "bash", "tool_search", "list_directory")))
            self.tool_runtime.install_tools(TOOL_DEFINITIONS, {
                **self.tool_functions, "edit": self._contextual_edit, "bash": self._contextual_bash,
                "memory_search": lambda query, include_history=False:
                    self.store.memory.search(query, include_history=include_history),
                "memory_manage": self._contextual_memory_manage,
            }, defaults=not supplied_runtime)
            policy = permission_policy
            if policy is None and not supplied_runtime:
                policy = PermissionPolicy(self.config.tool_permission_mode,
                                          max_timeout=self.config.tool_max_timeout)
            self.tool_runtime.configure(policy=policy, confirm=confirm_tool,
                                        recorder=self._record_audit, policy_recorder=self._policy_event)
            self.tool_runtime.attach_workspace(self.workspace)
            self.provider_session = ProviderSession(self.config.provider_tool_mode)
            self.provider_adapter = ToolProviderAdapter(self.tool_runtime, self.provider_session)
            self.native_loader = native_loader
            for registered in self.tool_runtime.stable_tools:
                name = str(registered.metadata.schema.get("name", registered.metadata.tool_id))
                self.tool_functions.setdefault(name, registered.handler)
            self._runtime_task_id = "0"
            self._restore_session()
            self.application.session_started(self)
        except BaseException:
            self.application.session_closed(self)
            self.store.close()
            if self._owns_application:
                self.application.close()
            raise

    def _contextual_memory_manage(self, context, action, fact=None, fact_id=None):
        from memory_authorization import authorize
        context.check()
        user = next((m for m in reversed(self.messages) if m.get('role') == 'user'), None)
        if not user:
            raise ValueError('memory management requires a current user event')
        quote = user.get('content', '')
        task = self.store.history.tasks[-1] if self.store.history.tasks else {}
        event = next((e for e in reversed(task.get('events', []))
                      if e.get('type') == 'message' and e.get('message', {}).get('role') == 'user'), None)
        if event is None:
            raise ValueError('memory management requires a recorded current user event')
        quote = event['message']['content']
        target = next((item for item in self.store.memory.facts(include_inactive=True)
                       if item['fact_id'] == fact_id), None) if fact_id else None
        authorize(self.memory_authorization_client, quote, action, fact, target)
        source = {'quote': quote, 'recorded_at': event['recorded_at'], 'source_task_id': event['task_id'],
                  'source_event_id': event['event_id'], 'trajectory_path': str(self.store.history.path)}
        if action == 'remember': return context.commit(lambda: self.store.memory.remember(fact or {}, source=source))
        if action == 'correct': return context.commit(lambda: self.store.memory.correct(fact_id, fact or {}, source=source))
        if action == 'forget': return context.commit(lambda: self.store.memory.forget(fact_id, source=source))
        raise ValueError('unknown memory action')

    def _save_runtime(self, audit=None):
        if self.store:
            self.store.record_runtime(dict(self.tool_runtime.snapshot(), provider=self.provider_session.snapshot()),
                                      audit=audit)

    def _record_audit(self, event):
        self._audit_events.append(deepcopy(event))
        self.store.record_tool_audit(event)

    def _policy_event(self, event):
        self._save_runtime(audit=event)
        self._audit_events.append(deepcopy(event))

    def _search_tools(self, query="", limit=8):
        result = self.tool_runtime.discover(self._runtime_task_id, query, limit)
        if "memory" not in str(query).casefold():
            result["tools"] = [item for item in result.get("tools", [])
                               if item.get("tool_id") not in {"memory_search", "memory_manage"}]
        self._save_runtime()
        return result

    def _ensure_dynamic_definitions(self):
        """Restore only active definitions when compaction retires their search result."""
        visible = set()
        for message in self.messages:
            if message.get("role") == "tool" and message.get("name") == "tool_search":
                try:
                    items = json.loads(message.get("content", "{}")).get("tools", [])
                    visible.update(item["schema_fingerprint"] for item in items if "schema" in item)
                except (ValueError, TypeError, KeyError, AttributeError):
                    pass
            elif message.get("role") == "system" and str(message.get("content", "")).startswith("Active tool definitions:\n"):
                items = json.loads(message["content"].split("\n", 1)[1])
                visible.update(item["schema_fingerprint"] for item in items)
        missing = [item for item in self.tool_runtime.active_definitions(self._runtime_task_id)
                   if item["schema_fingerprint"] not in visible and item["tool_id"] not in self.tool_functions]
        if not missing:
            return False
        self._append_message({"role": "system", "content": "Active tool definitions:\n" +
                              json.dumps(missing, ensure_ascii=False, sort_keys=True, separators=(",", ":"))})
        return True

    def _contextual_edit(self, context, **arguments):
        return self.workspace.edit(**arguments, execution_context=context)

    def _contextual_bash(self, context, **arguments):
        return self.workspace.bash(**arguments, execution_context=context)

    def _restore_session(self) -> None:
        """Load persisted history into this process, repairing interrupted rounds."""

        contents: SessionContents = self.store.load()
        self.tool_runtime.restore(contents.runtime)
        if contents.runtime.get("provider"):
            self.provider_session = ProviderSession(**contents.runtime["provider"])
            self.provider_adapter.session = self.provider_session
        for warning in contents.warnings:
            print(f"[会话恢复] {warning}")
        if not contents.messages:
            print(f"[会话] 新会话 {self.store.session_id}")
            return
        self.messages.extend(contents.messages)
        self.context.task_number = contents.task_number
        self.context.restore_session(contents.archive, contents.compressed_call_ids)
        restored = len(contents.messages)
        self._repair_interrupted_calls()
        last_user = next(
            (
                str(message.get("content", "")).replace("\n", " ")
                for message in reversed(self.messages)
                if message.get("role") == "user"
            ),
            "",
        )
        summary = (last_user[:60] + "…") if len(last_user) > 60 else last_user
        print(f"[会话恢复] {self.store.session_id}；消息 {restored} 条" + (f"；最后输入: {summary}" if summary else ""))

    def _repair_interrupted_calls(self) -> None:
        """Give unanswered tool calls a placeholder result instead of re-running them."""

        answered = {
            str(message.get("tool_call_id"))
            for message in self.messages
            if message.get("role") == "tool" and message.get("tool_call_id") is not None
        }
        missing: list[tuple[str, str]] = []
        for message in self.messages:
            if message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or []:
                if not isinstance(call, Mapping):
                    continue
                call_id = str(call.get("id") or "")
                if not call_id or call_id in answered:
                    continue
                function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
                missing.append((call_id, str(function.get("name", ""))))
                answered.add(call_id)
        for call_id, name in missing:
            payload = {"ok": False, "recovered": True, "error": "工具结果未完整记录，执行状态未知；恢复不会重放该调用。"}
            content = f"{CONTEXT_RECOVERED_MARKER}\n{json.dumps(payload, ensure_ascii=False)}"
            self._append_message({"role": "tool", "tool_call_id": call_id, "name": name, "content": content})
            print(f"[会话恢复] 工具调用 {name or '?'} ({call_id}) 缺少结果，已补写中断占位。")

    def _append_message(self, message: dict[str, Any]) -> None:
        if self.store is not None:
            self.store.record_message(message)
        self.messages.append(message)

    def close(self) -> None:
        if self.store is not None:
            self.store.close()
            self.store = None
        self.context.recorder = None
        self.application.session_closed(self)
        if self._owns_application:
            self.application.close()

    def _system_prompt(self) -> str:
        extensions = ", ".join(self.config.text_extensions)
        return (
            self.config.system_prompt + "\n" +
            f"默认工作区是 {self.config.root_dir}；read/read_file 可读取工作区外的文本文件，相对路径以工作区为基准；文本扩展名包括 {extensions}。\n"
            "tool_search 返回并激活工具定义，可在本次任务后续轮次调用；旧任务的定义不代表当前可调用。"
            "同批调用可用 _depends_on 指定前置调用 ID；参数值可用 "
            '{"$result":{"call_id":"前置ID","path":["字段"]}} 引用前置结果。'
        )

    @staticmethod
    def _tool_calls(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        calls = message.get("tool_calls")
        return list(calls) if isinstance(calls, list) else []

    @staticmethod
    def _normalise_args(raw):
        try:
            return ToolRuntime.normalise_args(raw)
        except ValueError as exc:
            raise WorkspaceError(str(exc)) from exc

    def _execute_tool(self, name, arguments, **kwargs):
        return self.tool_runtime.execute_model_call(self._runtime_task_id, name, arguments, **kwargs)

    def _complete_with_tools(self, messages, tools, choice):
        def native():
            if self.native_loader is None:
                raise ProviderLoadError("native deferred adapter is unsupported by this client")
            return self.native_loader(self.client, deepcopy(messages), deepcopy(tools),
                                      self.tool_runtime.active_definitions(self._runtime_task_id), choice)
        try:
            return self.provider_adapter.load(self._runtime_task_id, native,
                                              emulated_loader=lambda: self.client.complete(messages, tools, choice))
        except ProviderLoadError as exc:
            raise ModelRequestError(str(exc)) from exc
        except Exception as exc:
            raise ModelRequestError(str(exc)) from exc
        finally:
            self._save_runtime()

    def _tool_result_for_display(self, name: str, result: Mapping[str, Any]) -> str:
        """Render a compact terminal preview without changing the model result."""
        if self.config.verbose_tool_output:
            return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

        if not result.get("ok"):
            preview = f"tool={name} ok=false"
            if "exit_code" in result:
                preview += f" exit_code={result['exit_code']}"
            if result.get("output"):
                preview += f" output={str(result['output']).strip()}"
            preview += f" error={result.get('error', '工具调用失败')}"
        elif name == "list_directory":
            entries = result.get("entries", [])
            names = [str(entry.get("name", "?")) for entry in entries[:5] if isinstance(entry, Mapping)]
            suffix = "..." if len(entries) > 5 else ""
            preview = (
                f"ok=true path={result.get('path', '?')} entries={len(entries)}"
                f" [{', '.join(names)}{suffix}]"
            )
        elif name == "search_file_content":
            matches = result.get("matches", [])
            references = [
                f"{item.get('path', '?')}:{item.get('line', '?')}"
                for item in matches[:5]
                if isinstance(item, Mapping)
            ]
            suffix = "..." if len(matches) > 5 else ""
            preview = (
                f"ok=true query={result.get('query', '')!r} matches={len(matches)}"
                f" truncated={bool(result.get('truncated'))}"
                f" [{', '.join(references)}{suffix}]"
            )
        elif name == "read_file":
            content = str(result.get("content", "")).replace("\n", " ")
            preview = (
                f"ok=true path={result.get('path', '?')} "
                f"lines={result.get('start_line', '?')}-{result.get('end_line', '?')} "
                f"truncated={bool(result.get('truncated'))} preview={content}"
            )
        else:
            preview = json.dumps(result, ensure_ascii=False, separators=(",", ":"))

        limit = self.config.tool_output_preview_chars
        if len(preview) > limit:
            preview = preview[:limit].rstrip() + "..."
        return preview

    @staticmethod
    def _assistant_message(message: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
        if "reasoning_content" in message:
            result["reasoning_content"] = message["reasoning_content"]
        if message.get("tool_calls"):
            result["tool_calls"] = message["tool_calls"]
        return result

    def run_request(self, user_text: str) -> str | None:
        prefix = self.store.memory.task_prefix()
        self.messages[0] = {"role": "system", "content": prefix + "\n\n" + self._system_prompt()}
        task_system_message = dict(self.messages[0])
        request_offset = self.store.mark() if self.store is not None else None
        message_snapshot = deepcopy(self.messages)
        context_snapshot = self.context.snapshot()
        runtime_snapshot = self.tool_runtime.snapshot()
        audit_start = self.tool_runtime.audit_cursor()
        audit_event_start = len(self._audit_events)
        self.context.begin_task(user_text)
        recent = self.store.context_messages()
        if recent:
            if recent != self.messages[1:]:
                self.store.record_recent_context(recent)
                self.messages[1:] = recent
        self._runtime_task_id = f"{self.store.session_id}:{self.context.task_number}"
        self.tool_runtime.begin_task(self._runtime_task_id)
        self._save_runtime()
        self.usage_ledger.reset()
        self._append_message({"role": "user", "content": user_text})
        active_calls: list[Mapping[str, Any]] = []
        handled_call_indexes: set[int] = set()
        task_status = "interrupted"
        context_valid = True
        try:
            for round_number in range(1, self.config.max_rounds + 1):
                final_round = round_number == self.config.max_rounds
                if final_round:
                    self.messages[0] = {
                        **task_system_message,
                        "content": task_system_message["content"] + (
                            "\n\n当前是本次任务的最后一轮。请结合已有上下文和工具结果，直接回答用户原始请求。"
                            "本轮不能再使用工具；不要输出工具调用、调用标记（如 DSML）或继续执行的计划。"
                            "给出已有证据支持的答案，区分已确认的结果、尚未完成的部分和无法确认的信息；"
                            "信息不足时明确说明，不要编造结果或把失败的操作说成成功。"
                        ),
                    }
                self.context.set_round(round_number)
                print(f"\n[第 {round_number}/{self.config.max_rounds} 轮] 请求模型" + ("（收尾）" if final_round else ""))
                tools = [] if final_round else self.tool_runtime.schemas(self._runtime_task_id)
                overflow_retried = False
                while True:
                    request_messages = self.context.prepare_messages(self.messages, tools, self.compression_client)
                    if self._ensure_dynamic_definitions():
                        # Account for restored schemas without repeatedly compacting them away.
                        request_messages, total = self.context._prepare_request(self.messages, tools)
                        self.context.last_metrics.update(estimated_tokens=total,
                                                         over_budget=self.context.budget.over_budget(total))
                    metrics = self.context.last_metrics
                    print(
                        f"[上下文] 估算 {metrics.get('estimated_tokens', '?')} tokens"
                        + (
                            f" / 窗口 {metrics['window_tokens']} ({metrics.get('window_source', 'unknown')})"
                            if metrics.get("window_tokens")
                            else " / 窗口未配置"
                        )
                    )
                    if self.context.last_compression_event:
                        event = self.context.last_compression_event
                        print(
                            f"[上下文压缩/{event.reason}] {event.method}: "
                            f"{event.before_tokens} -> {event.after_tokens} tokens"
                            + (f"；{event.warning}" if event.warning else "")
                        )
                    if metrics.get("over_budget"):
                        raise ModelRequestError("压缩后输入仍超过可用输入预算；请缩小本次输入或开启新会话。")
                    try:
                        message = self._complete_with_tools(
                            request_messages,
                            tools,
                            "none" if final_round else "auto",
                        )
                        break
                    except ModelRequestError as exc:
                        if overflow_retried or not is_overflow_error(exc):
                            raise
                        overflow_retried = True
                        print("[溢出恢复] 服务端报告上下文超限，压缩后重试一次。")
                        recovered = self.context.compact(self.messages, tools, self.compression_client, OVERFLOW)
                        if not recovered.compacted:
                            raise
                self.context.record_usage(self.client.last_usage, request_messages, tools)
                assistant = self._assistant_message(message)
                self._append_message(assistant)
                calls = self._tool_calls(message)
                active_calls = calls
                handled_call_indexes = set()
                if not calls:
                    answer = message.get("content") or "模型没有返回文字回答。"
                    print(f"\nJarvis> {answer}")
                    task_status = "completed"
                    return str(answer)
                if final_round:
                    for call in calls:
                        function = call.get("function") or {}
                        self._append_message({"role": "tool", "tool_call_id": call.get("id", ""),
                                              "name": function.get("name", ""),
                                              "content": json.dumps(failure("round_limit"))})
                    print("已达到轮次上限，本次请求未完成。")
                    return None
                def record_result(item, result):
                    result_text = json.dumps(result, ensure_ascii=False)
                    print(f"[工具结果] {self._tool_result_for_display(item.tool_id, result)}")
                    self._append_message({"role": "tool", "tool_call_id": item.call_id,
                                          "name": item.tool_id, "content": result_text})
                    self.context.record_tool_result(item.tool_id, item.arguments, result, item.call_id)
                    handled_call_indexes.update(i for i, c in enumerate(calls) if c.get("id") == item.call_id)
                cancelled = self.tool_runtime.execute_batch(self._runtime_task_id, calls, record_result)
                if cancelled:
                    task_status = "cancelled"
                    active_calls = []
                    print("已取消当前请求。工具结果已保留。")
                    return None
                active_calls = []
            print("已达到轮次上限，本次请求未完成。")
            return None
        except KeyboardInterrupt:
            task_status = "cancelled"
            for call_index, call in enumerate(active_calls):
                if call_index in handled_call_indexes:
                    continue
                function_data = call.get("function", {}) if isinstance(call, Mapping) else {}
                name = function_data.get("name", "")
                call_id = call.get("id", "")
                result_text = json.dumps(
                    {"ok": False, "cancelled": True, "error": "工具调用因用户取消而未执行。"},
                    ensure_ascii=False,
                )
                self._append_message({"role": "tool", "tool_call_id": call_id, "name": name, "content": result_text})
                try:
                    normalised_arguments = self._normalise_args(function_data.get("arguments", {}))
                except WorkspaceError:
                    normalised_arguments = {}
                self.context.record_tool_result(
                    name,
                    normalised_arguments,
                    {"ok": False, "cancelled": True, "error": "工具调用因用户取消而未执行。"},
                    call_id,
                )
            print("\n已取消当前请求。已完成的工具结果已保留，未完成的调用不会被视为成功。")
            return None
        except ModelRequestError as exc:
            task_status = "failed"
            executed = self.tool_runtime.executed_since(audit_start)
            if not executed:
                context_valid = False
                # Policy/fallback decisions survive a failed request; activation does not.
                policy = self.tool_runtime.policy.snapshot()
                retained_runtime = dict(runtime_snapshot, policy=policy,
                                        provider=self.provider_session.snapshot())
                if self.store is not None and request_offset is not None:
                    self.store.rollback_context(request_offset, retained_runtime,
                                                self._audit_events[audit_event_start:])
                self.messages[:] = message_snapshot
                self.context.restore(context_snapshot)
                self.tool_runtime.restore(dict(runtime_snapshot, policy=policy))
                self.tool_runtime.end_task(self._runtime_task_id)
            print(f"模型请求失败: {exc}")
            return None
        finally:
            self.messages[0] = task_system_message
            self.store.end_task(task_status, context_valid=context_valid)
            self.usage_ledger.summary()

    def compact_now(self, keep_tokens: int | None = None, reason: str = MANUAL) -> CompactionResult:
        """Run an explicit compaction and report what it did.

        This is the same service the automatic trigger uses, so the state effect
        is identical; it does not start a task and does not touch task_number.
        """

        result = self.context.compact(
            self.messages, self.tool_runtime.schemas(self._runtime_task_id), self.compression_client, reason, keep_tokens=keep_tokens
        )
        if result.compacted:
            print(
                f"[压缩/{result.reason}] {result.before_tokens} -> {result.after_tokens} tokens；"
                f"退休 {result.retired_messages} 条旧消息（收起 {len(result.compressed_call_ids)} 个工具结果）；"
                f"方法={result.method}；保留窗口={result.keep_tokens}"
                + (f"；{result.warning}" if result.warning else "")
            )
        else:
            print(f"[压缩/{result.reason}] 未压缩：{result.warning}")
        return result


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Jarvis 第一阶段命令行 Agent")
    parser.add_argument("--env-file", type=Path, default=Path.cwd() / ".env", help="配置文件路径")
    parser.add_argument("--resume", nargs="?", const="", default=None,
                        help="恢复会话：不带值取当前工作区最近的会话，或指定会话标识")
    parser.add_argument("--list", action="store_true", dest="list_sessions",
                        help="列出当前工作区可恢复的会话")
    args = parser.parse_args(argv)
    try:
        config = Config.from_env(args.env_file)
    except ConfigurationError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    if args.list_sessions:
        sessions = SessionStore.list_sessions(config)
        if not sessions:
            print("当前工作区没有会话记录。")
            return 0
        print(f"当前工作区的会话（{config.root_dir}）：")
        for session in sessions:
            preview = session.last_user_text.replace("\n", " ")[:40]
            print(f"  {session.id}  {session.started_at}  消息 {session.message_count} 条"
                  + (f"  最后输入: {preview}" if preview else ""))
        return 0
    try:
        def confirm_tool(metadata, arguments):
            if not sys.stdin.isatty():
                return False
            try:
                return input(f"允许 {metadata.tool_id} ({metadata.risk}) {json.dumps(arguments, ensure_ascii=False)}? [y/N] ").strip().casefold() == "y"
            except (EOFError, KeyboardInterrupt):
                return False
        agent = Agent(config, resume=args.resume, confirm_tool=confirm_tool)
    except ConfigurationError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except (SessionNotFoundError, SessionLockedError) as exc:
        print(f"会话错误: {exc}", file=sys.stderr)
        return 2

    print("Jarvis 已启动。输入 exit 退出，/compact [保留token] 主动压缩上下文，/all、/safe、/wide 设置全局工具权限，Ctrl+C 取消当前请求。")
    try:
        while True:
            try:
                user_text = input("\nYou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n已退出。")
                return 0
            if not user_text:
                continue
            if user_text.casefold() == "exit":
                print("已退出。")
                return 0
            command, _, argument = user_text.partition(" ")
            if command.casefold() == "/compact":
                try:
                    keep = parse_compact_argument(argument)
                except ValueError as exc:
                    print(str(exc))
                    continue
                agent.compact_now(keep)
                continue
            if command.casefold() == "/permissions":
                print(f"全局工具权限：{agent.tool_runtime.policy.mode}")
                continue
            mode = PERMISSION_COMMANDS.get(command.casefold())
            if mode:
                policy = agent.tool_runtime.policy
                widening = PermissionPolicy.MODES[mode] > PermissionPolicy.MODES[policy.mode]
                if widening:
                    try:
                        confirmed = input(f"将全局工具权限升级为 {mode}，后续启动均会使用此设置。确认? [y/N] ").strip().casefold() == "y"
                    except (EOFError, KeyboardInterrupt):
                        confirmed = False
                    if not confirmed:
                        print("未更改全局工具权限。")
                        continue
                try:
                    save_global_tool_permission_mode(resolve_state_dir(config), mode)
                except OSError as exc:
                    print(f"无法保存全局工具权限：{exc}")
                    continue
                outcome = policy.change_mode(mode, confirmed=widening)
                if not outcome["ok"]:
                    print(f"未更改全局工具权限：{outcome['error']['message']}")
                    continue
                print(f"全局工具权限已设为 {mode}。")
                continue
            try:
                agent.run_request(user_text)
            except ProfileEditError as exc:
                print(f"[记忆编辑未导入] {exc}；请修正 memory.md 后重试。原文件已保留。")
    finally:
        agent.close()


if __name__ == "__main__":
    raise SystemExit(main())
