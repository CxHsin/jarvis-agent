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
from copy import deepcopy
from urllib.parse import quote
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event
from typing import Any, Callable, Mapping, Sequence

from context_manager import CONTEXT_RECOVERED_MARKER, ContextManager, estimate_tokens
from cache_metrics import MeasuredClient, UsageLedger
from compaction import CompactionResult, MANUAL, OVERFLOW, is_overflow_error
from context_budget import ContextBudget
from model_capabilities import load_capability
from session_store import SessionContents, SessionLockedError, SessionNotFoundError, SessionStore
from tool_runtime import (ToolMetadata, ToolRegistry, ToolRuntime, ToolCall, ToolScheduler,
                          PermissionPolicy, ProviderSession, ToolProviderAdapter, ProviderLoadError, failure)


DEFAULT_TEXT_EXTENSIONS = (".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".py")


class ConfigurationError(ValueError):
    """Raised when the local configuration cannot start the agent."""


class ModelRequestError(RuntimeError):
    """Raised when the model endpoint cannot complete a request."""


class WorkspaceError(ValueError):
    """Raised for invalid or unsupported workspace operations."""


def _read_dotenv(path: Path) -> dict[str, str]:
    """Read the small dotenv subset needed by this project without a dependency."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _setting(values: Mapping[str, str], key: str, default: str | None = None) -> str | None:
    environment_value = os.environ.get(key)
    if environment_value is not None:
        return environment_value
    return values.get(key, default)


def parse_compact_argument(argument: str) -> int | None:
    """Return the optional keep target for the /compact command."""

    text = argument.strip()
    if not text:
        return None
    if not text.isdigit() or int(text) < 1:
        raise ValueError("用法：/compact [保留的 token 数]，例如 /compact 2000；不带参数时保留当前任务的原文。")
    return int(text)


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str
    model: str
    max_rounds: int = 5
    root_dir: Path = field(default_factory=Path.cwd)
    state_dir: Path | None = None
    recent_task_count: int = 5
    text_extensions: tuple[str, ...] = DEFAULT_TEXT_EXTENSIONS
    request_timeout: float = 60.0
    max_read_chars: int = 12_000
    max_search_matches: int = 20
    max_directory_entries: int = 200
    max_tool_result_tokens: int = 8_192
    compression_model: str | None = None
    context_window_tokens: int | None = None
    context_window_source: str = "unknown"
    context_reserve_tokens: int = 16_384
    max_output_tokens: int = 32768
    model_max_output_tokens: int | None = None
    model_max_input_tokens: int | None = None
    model_capabilities_file: Path | None = None
    capability_source: str = "unknown"
    capability_checked_at: str = "unknown"
    compression_context_window_tokens: int | None = None
    compression_max_output_tokens: int | None = None
    context_summary_max_chars: int = 6_000
    context_keep_recent_tokens: int = 20_000
    context_compaction_failure_limit: int = 3
    tool_output_preview_chars: int = 500
    verbose_tool_output: bool = False
    tool_permission_mode: str = "approve-dangerous"
    provider_tool_mode: str = "emulated"
    tool_max_timeout: float = 60.0

    def __post_init__(self) -> None:
        if self.recent_task_count < 1:
            raise ConfigurationError("RECENT_TASK_COUNT 必须大于 0。")
        if self.tool_permission_mode not in PermissionPolicy.MODES:
            raise ConfigurationError("无效的 TOOL_PERMISSION_MODE。")
        if self.provider_tool_mode not in {"native", "emulated"}:
            raise ConfigurationError("PROVIDER_TOOL_MODE 必须是 native 或 emulated。")
        if not 0 < self.tool_max_timeout <= 3600:
            raise ConfigurationError("TOOL_MAX_TIMEOUT 必须大于 0 且不超过 3600 秒。")
        if self.context_window_tokens is not None and self.context_window_source == "unknown":
            object.__setattr__(self, "context_window_source", "configured")

    @classmethod
    def from_env(cls, dotenv_path: Path | None = None) -> "Config":
        values = _read_dotenv(dotenv_path or Path.cwd() / ".env")
        base_url = _setting(values, "BASE_URL")
        model = _setting(values, "MODEL")
        if not base_url:
            raise ConfigurationError("缺少 BASE_URL，请在 .env 中配置 OpenAI 兼容接口地址。")
        if not model:
            raise ConfigurationError("缺少 MODEL，请在 .env 中配置模型名。")

        def positive_int(name: str, default: int) -> int:
            raw = _setting(values, name, str(default))
            try:
                result = int(raw or default)
            except ValueError as exc:
                raise ConfigurationError(f"{name} 必须是正整数。") from exc
            if result < 1:
                raise ConfigurationError(f"{name} 必须是正整数。")
            return result

        def optional_positive_int(name: str) -> int | None:
            raw = _setting(values, name)
            if raw is None or not raw.strip():
                return None
            try:
                result = int(raw)
            except ValueError as exc:
                raise ConfigurationError(f"{name} 必须是正整数。") from exc
            if result < 1:
                raise ConfigurationError(f"{name} 必须是正整数。")
            return result

        def boolean(name: str, default: bool = False) -> bool:
            raw = _setting(values, name)
            if raw is None:
                return default
            normalised = raw.strip().casefold()
            if normalised in {"1", "true", "yes", "on"}:
                return True
            if normalised in {"0", "false", "no", "off"}:
                return False
            raise ConfigurationError(f"{name} 必须是 true/false。")

        root_value = _setting(values, "ROOT_DIR", str(Path.cwd()))
        root_dir = Path(root_value or Path.cwd()).expanduser().resolve()
        if not root_dir.exists() or not root_dir.is_dir():
            raise ConfigurationError(f"ROOT_DIR 不是可访问的目录: {root_dir}")
        state_value = _setting(values, "STATE_DIR")
        state_dir = Path(state_value).expanduser().resolve() if state_value else None

        extensions_value = _setting(values, "TEXT_EXTENSIONS", ",".join(DEFAULT_TEXT_EXTENSIONS))
        extensions = tuple(
            extension if extension.startswith(".") else f".{extension}"
            for extension in (item.strip().lower() for item in (extensions_value or "").split(","))
            if extension
        )
        if not extensions:
            raise ConfigurationError("TEXT_EXTENSIONS 至少需要一种文件扩展名。")

        timeout_raw = _setting(values, "REQUEST_TIMEOUT", "60")
        try:
            timeout = float(timeout_raw or "60")
        except ValueError as exc:
            raise ConfigurationError("REQUEST_TIMEOUT 必须是数字。") from exc
        if timeout <= 0:
            raise ConfigurationError("REQUEST_TIMEOUT 必须大于 0。")

        context_window = optional_positive_int("CONTEXT_WINDOW_TOKENS")
        return cls(
            base_url=base_url.rstrip("/"),
            api_key=_setting(values, "API_KEY", "") or "",
            model=model,
            max_rounds=positive_int("MAX_ROUNDS", 5),
            root_dir=root_dir,
            state_dir=state_dir,
            tool_permission_mode=_setting(values, "TOOL_PERMISSION_MODE", "approve-dangerous"),
            provider_tool_mode=_setting(values, "PROVIDER_TOOL_MODE", "emulated"),
            tool_max_timeout=positive_int("TOOL_MAX_TIMEOUT", 60),
            text_extensions=extensions,
            request_timeout=timeout,
            max_read_chars=positive_int("MAX_READ_CHARS", 12_000),
            max_search_matches=positive_int("MAX_SEARCH_MATCHES", 20),
            max_directory_entries=positive_int("MAX_DIRECTORY_ENTRIES", 200),
            max_tool_result_tokens=positive_int("MAX_TOOL_RESULT_TOKENS", 8_192),
            compression_model=_setting(values, "COMPRESSION_MODEL") or None,
            context_window_tokens=context_window,
            context_window_source="configured" if context_window is not None else "unknown",
            context_reserve_tokens=positive_int("CONTEXT_RESERVE_TOKENS", 16_384),
            max_output_tokens=positive_int("MAX_OUTPUT_TOKENS", 32768),
            model_capabilities_file=Path(_setting(values, "MODEL_CAPABILITIES_FILE")).expanduser().resolve()
            if _setting(values, "MODEL_CAPABILITIES_FILE") else None,
            compression_context_window_tokens=optional_positive_int("COMPRESSION_CONTEXT_WINDOW_TOKENS"),
            compression_max_output_tokens=optional_positive_int("COMPRESSION_MAX_OUTPUT_TOKENS"),
            context_summary_max_chars=positive_int("CONTEXT_SUMMARY_MAX_CHARS", 6_000),
            context_keep_recent_tokens=positive_int("CONTEXT_KEEP_RECENT_TOKENS", 20_000),
            recent_task_count=positive_int("RECENT_TASK_COUNT", 5),
            context_compaction_failure_limit=positive_int("CONTEXT_COMPACTION_FAILURE_LIMIT", 3),
            tool_output_preview_chars=positive_int("TOOL_OUTPUT_PREVIEW_CHARS", 500),
            verbose_tool_output=boolean("VERBOSE_TOOL_OUTPUT"),
        )


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
        import subprocess
        import tempfile
        import time
        if not command or not command.strip(): raise WorkspaceError("command 不能为空。")
        deadline = time.monotonic() + min(float(timeout), 60.0)
        with tempfile.TemporaryFile() as output_file:
            def start():
                return subprocess.Popen(command, cwd=self.root, shell=True, stdout=output_file, stderr=output_file,
                                        start_new_session=os.name != "nt",
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            process = execution_context.commit(start) if execution_context else start()
            try:
                while process.poll() is None:
                    if execution_context:
                        execution_context.check()
                    if time.monotonic() >= deadline:
                        raise TimeoutError("命令执行超时。")
                    time.sleep(0.01)
            finally:
                if process.poll() is None:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                    else:
                        import signal
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            output_file.seek(0)
            limit = max(1000, self.config.max_read_chars)
            data = output_file.read(limit + 1)
        return {"ok": process.returncode == 0, "exit_code": process.returncode,
                "output": data[:limit].decode("utf-8", errors="replace"), "truncated": len(data) > limit}


TOOL_DEFINITIONS: list[dict[str, Any]] = [
 {"type":"function","function":{"name":"read","description":"读取文本文件；支持工作区外的绝对路径，相对路径以工作区为基准。","parameters":{"type":"object","properties":{"path":{"type":"string"},"start_line":{"type":"integer","minimum":1},"end_line":{"type":"integer","minimum":1}},"required":["path"],"additionalProperties":False}}},
 {"type":"function","function":{"name":"edit","description":"编辑工作区文本文件。","parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"},"start_line":{"type":"integer","minimum":1},"end_line":{"type":"integer","minimum":1}},"required":["path","content"],"additionalProperties":False}}},
 {"type":"function","function":{"name":"bash","description":"在工作区执行 shell 命令。","parameters":{"type":"object","properties":{"command":{"type":"string"},"timeout":{"type":"number","minimum":0.1,"maximum":60}},"required":["command"],"additionalProperties":False}}},
 {"type":"function","function":{"name":"tool_search","description":"搜索可用工具。","parameters":{"type":"object","properties":{"query":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":20}},"additionalProperties":False}}},
]

TOOL_DEFINITIONS[1]["function"]["parameters"]["properties"]["expected_hash"] = {
    "type": "string", "description": "先前读取的 SHA-256；新文件使用 missing。"}
for _definition in TOOL_DEFINITIONS:
    _properties = _definition["function"]["parameters"]["properties"]
    _properties["_depends_on"] = {"type": "array", "items": {"type": "string"},
                                  "description": "同一批次内必须先成功的 tool_call IDs。"}
    _properties["_version"] = {"type": "string"}
    _properties["_schema_fingerprint"] = {"type": "string"}



class ChatCompletionsClient:
    CONTEXT_WINDOW_KEYS = (
        "context_window",
        "context_length",
        "max_context_length",
        "max_model_len",
    )

    def __init__(self, config: Config):
        self.config = config
        self.last_usage: dict[str, Any] | None = None

    def _endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    def _models_endpoint(self, model: str | None = None) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        suffix = f"/{quote(model, safe='')}" if model else ""
        return f"{base}/models{suffix}"

    @classmethod
    def _extract_context_window(cls, payload: Any) -> int | None:
        """Read common provider metadata fields without guessing from model names."""
        if isinstance(payload, Mapping):
            for key in cls.CONTEXT_WINDOW_KEYS:
                value = payload.get(key)
                if isinstance(value, bool):
                    continue
                if not (type(value) is int or isinstance(value, str) and value.isdecimal()):
                    continue
                parsed = int(value)
                if parsed > 0:
                    return parsed
        return None

    def discover_context_window(self) -> int | None:
        """Best-effort discovery from OpenAI-compatible model metadata.

        The compatibility API does not require a context-window field, so all
        discovery failures intentionally fall back to manual configuration.
        """
        headers = {
            **({"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}),
            "Accept": "application/json",
        }
        timeout = min(self.config.request_timeout, 5.0)
        endpoints = [self._models_endpoint(self.config.model), self._models_endpoint()]
        for endpoint in endpoints:
            request = urllib.request.Request(endpoint, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, Mapping) and isinstance(payload.get("data"), list):
                matching = [item for item in payload["data"]
                            if isinstance(item, Mapping) and item.get("id") == self.config.model]
                payload = matching[0] if len(matching) == 1 else None
            elif endpoint == endpoints[-1]:
                payload = None
            elif isinstance(payload, Mapping) and payload.get("id", self.config.model) != self.config.model:
                payload = None
            discovered = self._extract_context_window(payload)
            if discovered is not None:
                return discovered
        return None

    def resolve_config(self) -> Config:
        config = self.config
        try:
            capability = load_capability(config.base_url, config.model, config.model_capabilities_file)
            if not capability and config.model_capabilities_file:
                capability = load_capability(config.base_url, config.model)
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc
        window = config.context_window_tokens
        source = config.context_window_source
        if window is None and capability:
            window = capability["context_window_tokens"]
            source = "catalog"
        if window is None:
            window = self.discover_context_window()
            source = "upstream" if window else "unknown"
        if window is None:
            raise ConfigurationError(f"模型 {config.model} 的窗口未知，请设置 CONTEXT_WINDOW_TOKENS 或模型能力配置。")
        output_limit = capability.get("max_output_tokens")
        config = replace(config, context_window_tokens=window, context_window_source=source,
                         model_max_output_tokens=output_limit,
                         model_max_input_tokens=capability.get("max_input_tokens"),
                         max_output_tokens=min(config.max_output_tokens, output_limit) if output_limit else config.max_output_tokens,
                         capability_source=capability.get("source", source),
                         capability_checked_at=capability.get("checked_at", "unknown"))
        try:
            ContextBudget.from_config(config).validate(label=f"模型 {config.model}")
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc
        self.config = config
        return config

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        tool_choice: str = "auto",
    ) -> dict[str, Any]:
        self.last_usage = None
        budget = ContextBudget.from_config(self.config)
        estimated = estimate_tokens(messages, tools)
        if budget.over_budget(estimated):
            raise ModelRequestError("预计输入超过可用输入预算，压缩不足；请缩小本次输入或开启新会话。")
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": list(messages),
            "temperature": 0,
            "max_tokens": budget.output_limit(estimated),
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = tool_choice
        request = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.request_timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ModelRequestError(f"模型接口返回 HTTP {exc.code}: {detail[:1000]}") from exc
        except urllib.error.URLError as exc:
            raise ModelRequestError(f"无法连接模型接口: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ModelRequestError("模型请求超时。") from exc
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ModelRequestError("模型接口返回的不是有效 JSON。") from exc
        if not isinstance(data, dict) or not data.get("choices"):
            raise ModelRequestError(f"模型接口响应缺少 choices: {json.dumps(data, ensure_ascii=False)[:1000]}")
        usage = data.get("usage")
        if isinstance(usage, dict):
            self.last_usage = usage
        choice = data["choices"][0]
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            raise ModelRequestError("模型接口响应缺少 choices[0].message。")
        return message


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
    ):
        supplied_runtime = tool_runtime is not None
        self.client = client or ChatCompletionsClient(config)
        self.store = store
        if self.store is None and resume is not None:
            # Fail on a missing or busy session before touching the model.
            self.store = SessionStore.resume(config, resume or None)
        self.config = self.client.resolve_config() if isinstance(self.client, ChatCompletionsClient) else config
        if isinstance(self.client, ChatCompletionsClient):
            print(f"[模型容量] {config.model}: 窗口={self.config.context_window_tokens} "
                  f"输出上限={self.config.max_output_tokens} 窗口来源={self.config.context_window_source} "
                  f"能力来源={self.config.capability_source} "
                  f"核对日期={self.config.capability_checked_at}")
        self.workspace = Workspace(self.config)
        self.tool_registry = tool_runtime.registry if tool_runtime is not None else ToolRegistry()
        if compression_client is not None:
            self.compression_client = compression_client
            if isinstance(compression_client, ChatCompletionsClient):
                compression_client.resolve_config()
        elif isinstance(self.client, ChatCompletionsClient) and (
            config.compression_model and config.compression_model != config.model
            or config.compression_max_output_tokens is not None
            or config.compression_context_window_tokens is not None
        ):
            # 压缩客户端只生成摘要，不参与主上下文的切点保留，因此不继承主模型的保留窗口。
            compression_config = replace(config, model=config.compression_model or config.model,
                                         context_window_tokens=config.compression_context_window_tokens,
                                         context_window_source="configured" if config.compression_context_window_tokens else "unknown",
                                         max_output_tokens=config.compression_max_output_tokens or config.max_output_tokens,
                                         context_keep_recent_tokens=1)
            self.compression_client = ChatCompletionsClient(compression_config)
            self.compression_client.resolve_config()
        else:
            self.compression_client = self.client
        self.usage_ledger = UsageLedger()
        self.compression_client = MeasuredClient(self.compression_client, self.usage_ledger,
                                                "压缩模型", config.compression_model or config.model)
        self.client = MeasuredClient(self.client, self.usage_ledger, "主模型", config.model)
        if self.store is None:
            self.store = SessionStore.create(self.config)
        self._audit_events = []
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": self._system_prompt()}]
        self.context = ContextManager(self.config, recorder=self.store)
        self.tool_functions: dict[str, ToolFunction] = {
            "read": self.workspace.read, "edit": self.workspace.edit, "bash": self.workspace.bash,
            "tool_search": self._search_tools,
            "list_directory": self.workspace.list_directory, "search_file_content": self.workspace.search_file_content, "read_file": self.workspace.read_file,
        }
        if tool_runtime is None:
            for definition in TOOL_DEFINITIONS:
                schema = definition["function"]
                self.tool_registry.register(
                    ToolMetadata(tool_id=str(schema["name"]), version="1", schema=schema,
                                 risk="high" if schema["name"] == "bash" else "medium" if schema["name"] == "edit" else "low",
                                 side_effects=("filesystem",) if schema["name"] in {"edit", "bash"} else (),
                                 concurrency="parallel" if schema["name"] in {"read", "edit"} else "serial"),
                    self._contextual_edit if schema["name"] == "edit" else self._contextual_bash if schema["name"] == "bash" else self.tool_functions[str(schema["name"])],
                    contextual=schema["name"] in {"edit", "bash"},
                )
            for alias in ("list_directory", "search_file_content", "read_file"):
                if (alias, "1") not in self.tool_registry._tools:
                    self.tool_registry.register(ToolMetadata(alias, "1", {"name": alias, "description": "Legacy alias", "parameters": {"type": "object", "additionalProperties": True}}), self.tool_functions[alias])
            tool_runtime = ToolRuntime(
                self.tool_registry,
                stable=tuple((name, "1") for name in ("read", "edit", "bash", "tool_search")),
            )
        self.tool_runtime = tool_runtime
        self.tool_runtime.policy = permission_policy or self.tool_runtime.policy
        if permission_policy is None and not supplied_runtime:
            self.tool_runtime.policy = PermissionPolicy(self.config.tool_permission_mode,
                                                       max_timeout=self.config.tool_max_timeout)
        self.tool_runtime.policy.on_event = self._policy_event
        self.tool_runtime.dispatcher.policy = self.tool_runtime.policy
        self.tool_runtime.dispatcher.confirm = confirm_tool
        self.tool_runtime.dispatcher.recorder = self._record_audit
        self.tool_runtime.dispatcher.resource_resolver = self._tool_resources
        self.provider_session = ProviderSession(self.config.provider_tool_mode)
        self.provider_adapter = ToolProviderAdapter(self.tool_runtime, self.provider_session)
        self.native_loader = native_loader
        for registered in self.tool_runtime.stable_tools:
            name = str(registered.metadata.schema.get("name", registered.metadata.tool_id))
            self.tool_functions.setdefault(name, registered.handler)
        self._runtime_task_id = "0"
        self._restore_session()
        self.store.memory.start_worker(extraction_client or ChatCompletionsClient(self.config))

    def _save_runtime(self):
        if self.store:
            self.store.record_runtime(dict(self.tool_runtime.snapshot(), provider=self.provider_session.snapshot()))

    def _record_audit(self, event):
        self._audit_events.append(deepcopy(event))
        self.store.record_tool_audit(event)

    def _policy_event(self, event):
        self._record_audit(event)
        self._save_runtime()

    def _search_tools(self, query="", limit=8):
        result = self.tool_runtime.discover(self._runtime_task_id, query, limit)
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

    def _tool_resources(self, metadata, arguments):
        if metadata.tool_id in {"read", "read_file", "edit"}:
            path = arguments.get("path")
            if not isinstance(path, str):
                return {"*"}
            return {"file:" + os.path.normcase(str(self.workspace._resolve_read(path)))}
        return set()

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
        self.messages.append(message)
        if self.store is not None:
            self.store.record_message(message)

    def close(self) -> None:
        if self.store is not None:
            self.store.close()
            self.store = None
        self.context.recorder = None

    def _system_prompt(self) -> str:
        extensions = ", ".join(self.config.text_extensions)
        return (
            "你是 Jarvis，一个通用的个人助理。当前阶段可以使用文件工具完成用户请求。\n"
            f"默认工作区是 {self.config.root_dir}；read/read_file 可读取工作区外的文本文件，相对路径以工作区为基准；文本扩展名包括 {extensions}。\n"
            "需要文件信息时先使用工具，不要凭空猜测。工具返回的失败不能证明内容不存在。"
            "回答时区分已确认的事实和不确定性，并使用用户的语言。"
            "tool_search 返回并激活工具定义，可在本次任务后续轮次调用；旧任务的定义不代表当前可调用。"
            "同批调用可用 _depends_on 指定前置调用 ID；参数值可用 "
            '{"$result":{"call_id":"前置ID","path":["字段"]}} 引用前置结果。'
        )

    @staticmethod
    def _tool_calls(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        calls = message.get("tool_calls")
        return list(calls) if isinstance(calls, list) else []

    @staticmethod
    def _normalise_args(raw: Any) -> dict[str, Any]:
        if raw is None or raw == "":
            return {}
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raise WorkspaceError("工具参数不是有效 JSON。")
        if not isinstance(parsed, dict):
            raise WorkspaceError("工具参数必须是 JSON 对象。")
        return parsed

    def _execute_tool(self, name: str, arguments: Any, *, version=None, fingerprint=None,
                      call_id="", cancellation=None, authorization=None) -> dict[str, Any]:
        try:
            args = self._normalise_args(arguments)
            key, error = self.tool_runtime.binding(self._runtime_task_id, name, version, fingerprint)
            if error:
                self._record_audit(dict(error, tool_id=name, call_id=call_id, phase="rejected"))
                return error
            result = self.tool_runtime.execute(self._runtime_task_id, *key, args, fingerprint=fingerprint,
                                               call_id=call_id, cancellation=cancellation,
                                               authorization=authorization)
            if result.get("ok"):
                payload = result.get("result")
                return payload if isinstance(payload, dict) else {"ok": True, "value": payload}
            return {k: v for k, v in result.items() if k != "audit"}
        except (TypeError, WorkspaceError, OSError, ValueError) as exc:
            return failure("invalid_arguments", str(exc))

    def _execute_batch(self, calls, on_result):
        prepared = []
        errors = {}
        cancellation = Event()
        for index, raw in enumerate(calls):
            function = raw.get("function") or {}
            name, cid = function.get("name", ""), str(raw.get("id") or f"missing-{index}")
            try:
                args = dict(self._normalise_args(function.get("arguments", {})))
                dependencies = args.pop("_depends_on", raw.get("depends_on", ()))
                version = args.pop("_version", raw.get("version"))
                fingerprint = args.pop("_schema_fingerprint", raw.get("schema_fingerprint"))
                if not isinstance(dependencies, (list, tuple)) or not all(isinstance(x, str) for x in dependencies):
                    raise ValueError("_depends_on must be an array of call IDs")
                key, error = self.tool_runtime.binding(self._runtime_task_id, name, version, fingerprint)
                if error:
                    errors[cid] = error
                elif name == "edit" and isinstance(args.get("path"), str) and "expected_hash" not in args:
                    target = self.workspace._resolve(args["path"])
                    args["expected_hash"] = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else "missing"
                prepared.append(ToolCall(cid, name, key[1] if key else version or "1", args,
                                         tuple(dependencies), fingerprint))
            except (ValueError, TypeError, OSError) as exc:
                errors[cid] = failure("invalid_arguments", str(exc))
                prepared.append(ToolCall(cid, name, "1"))
        if errors:
            # Reject the batch before effects when its dependency graph cannot be trusted.
            for item in prepared:
                result = errors.get(item.call_id, failure("batch_rejected", "another call is invalid"))
                self._record_audit(dict(result, call_id=item.call_id, tool_id=item.tool_id, phase="rejected"))
                on_result(item, result)
            return False

        def execute(item, arguments, token, grant):
            payload = self._execute_tool(item.tool_id, arguments, version=item.version,
                                         fingerprint=item.fingerprint, call_id=item.call_id, cancellation=token,
                                         authorization=grant)
            return {"ok": True, "result": payload} if payload.get("ok", True) else payload

        batch_audit_start = len(self.tool_runtime.dispatcher.audit)

        def record(item, result):
            if not any(e.get("call_id") == item.call_id and e.get("phase") == "finished"
                       for e in self.tool_runtime.dispatcher.audit[batch_audit_start:]):
                self.tool_runtime.dispatcher.record({"call_id": item.call_id, "tool_id": item.tool_id,
                                                     "phase": "finished", "ok": result.get("ok", False),
                                                     "error": result.get("error"),
                                                     "policy_version": self.tool_runtime.policy.version})
            on_result(item, result.get("result") if result.get("ok") else result)

        scheduler = ToolScheduler(self.tool_registry, dispatcher=self.tool_runtime.dispatcher)
        outcome = scheduler.execute(prepared, cancellation=cancellation, execute=execute, on_result=record)
        if not outcome["ok"]:
            for item in prepared:
                self._record_audit(dict(outcome, call_id=item.call_id, phase="rejected"))
                on_result(item, outcome)
        return cancellation.is_set()

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
            preview = f"ok=false error={result.get('error', '工具调用失败')}"
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
        request_offset = self.store.mark() if self.store is not None else None
        message_snapshot = deepcopy(self.messages)
        context_snapshot = self.context.snapshot()
        runtime_snapshot = self.tool_runtime.snapshot()
        provider_snapshot = self.provider_session.snapshot()
        audit_start = len(self.tool_runtime.dispatcher.audit)
        audit_event_start = len(self._audit_events)
        self.context.begin_task(user_text)
        recent = self.store.recent_messages()
        if recent:
            self.messages[1:] = recent
            self.store.record_recent_context(recent)
        self._runtime_task_id = f"{self.store.session_id}:{self.context.task_number}"
        compatibility = tuple((name, "1") for name in ("list_directory", "search_file_content", "read_file")
                              if self.tool_registry.versions(name))
        self.tool_runtime.begin_task(self._runtime_task_id, compatibility)
        self._save_runtime()
        self.usage_ledger.reset()
        self._append_message({"role": "user", "content": user_text})
        active_calls: list[Mapping[str, Any]] = []
        handled_call_indexes: set[int] = set()
        task_status = "interrupted"
        try:
            for round_number in range(1, self.config.max_rounds + 1):
                final_round = round_number == self.config.max_rounds
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
                cancelled = self._execute_batch(calls, record_result)
                if cancelled:
                    active_calls = []
                    print("已取消当前请求。工具结果已保留。")
                    return None
                active_calls = []
            print("已达到轮次上限，本次请求未完成。")
            return None
        except KeyboardInterrupt:
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
            executed = any(e.get("phase") == "started" for e in self.tool_runtime.dispatcher.audit[audit_start:])
            if not executed:
                task_status = "failed"
                self.messages[:] = message_snapshot
                self.context.restore(context_snapshot)
                # Policy/fallback decisions survive a failed request; activation does not.
                policy = self.tool_runtime.policy.snapshot()
                self.tool_runtime.restore(dict(runtime_snapshot, policy=policy))
                self.tool_registry._tasks.pop(self._runtime_task_id, None)
                if self.store is not None and request_offset is not None:
                    self.store.truncate_to(request_offset)
                    for event in self._audit_events[audit_event_start:]:
                        self.store.record_tool_audit(event)
                if self.provider_session.snapshot() != provider_snapshot or self.tool_runtime.policy.snapshot() != runtime_snapshot["policy"]:
                    self._save_runtime()
            print(f"模型请求失败: {exc}")
            return None
        finally:
            self.store.end_task(task_status)
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

    print("Jarvis 已启动。输入 exit 退出，/compact [保留token] 主动压缩上下文，Ctrl+C 取消当前请求。")
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
            agent.run_request(user_text)
    finally:
        agent.close()


if __name__ == "__main__":
    raise SystemExit(main())
