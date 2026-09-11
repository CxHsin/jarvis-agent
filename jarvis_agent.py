"""Stage-one command-line personal agent.

The implementation deliberately keeps the agent loop visible: a model response
may request tools, each tool call is executed in order, and the results are
returned to the model until it answers or the round limit is reached.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.request
from copy import deepcopy
from urllib.parse import quote
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from context_manager import CONTEXT_RECOVERED_MARKER, ContextManager, estimate_tokens
from cache_metrics import MeasuredClient, UsageLedger
from model_capabilities import load_capability
from session_store import SessionContents, SessionLockedError, SessionNotFoundError, SessionStore


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


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str
    model: str
    max_rounds: int = 5
    root_dir: Path = field(default_factory=Path.cwd)
    state_dir: Path | None = None
    text_extensions: tuple[str, ...] = DEFAULT_TEXT_EXTENSIONS
    request_timeout: float = 60.0
    max_read_chars: int = 12_000
    max_search_matches: int = 20
    compression_model: str | None = None
    context_window_tokens: int | None = None
    context_window_source: str = "unknown"
    context_compression_threshold: float = 0.90
    context_safety_margin: float = 0.02
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

    def __post_init__(self) -> None:
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

        def ratio(name: str, default: float) -> float:
            raw = _setting(values, name, str(default))
            try:
                result = float(raw or default)
            except ValueError as exc:
                raise ConfigurationError(f"{name} 必须是 0 到 1 之间的小数。") from exc
            if not 0 < result < 1:
                raise ConfigurationError(f"{name} 必须是 0 到 1 之间的小数。")
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

        context_threshold = ratio("CONTEXT_COMPRESSION_THRESHOLD", 0.90)
        context_window = optional_positive_int("CONTEXT_WINDOW_TOKENS")
        return cls(
            base_url=base_url.rstrip("/"),
            api_key=_setting(values, "API_KEY", "") or "",
            model=model,
            max_rounds=positive_int("MAX_ROUNDS", 5),
            root_dir=root_dir,
            state_dir=state_dir,
            text_extensions=extensions,
            request_timeout=timeout,
            max_read_chars=positive_int("MAX_READ_CHARS", 12_000),
            max_search_matches=positive_int("MAX_SEARCH_MATCHES", 20),
            compression_model=_setting(values, "COMPRESSION_MODEL") or None,
            context_window_tokens=context_window,
            context_window_source="configured" if context_window is not None else "unknown",
            context_compression_threshold=context_threshold,
            context_safety_margin=ratio("CONTEXT_SAFETY_MARGIN", 0.02),
            max_output_tokens=positive_int("MAX_OUTPUT_TOKENS", 32768),
            model_capabilities_file=Path(_setting(values, "MODEL_CAPABILITIES_FILE")).expanduser().resolve()
            if _setting(values, "MODEL_CAPABILITIES_FILE") else None,
            compression_context_window_tokens=optional_positive_int("COMPRESSION_CONTEXT_WINDOW_TOKENS"),
            compression_max_output_tokens=optional_positive_int("COMPRESSION_MAX_OUTPUT_TOKENS"),
            context_summary_max_chars=positive_int("CONTEXT_SUMMARY_MAX_CHARS", 6_000),
            context_keep_recent_tokens=positive_int("CONTEXT_KEEP_RECENT_TOKENS", 20_000),
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
        return {"ok": True, "path": _display_path(directory, self.root), "entries": entries}

    def _is_text_file(self, path: Path) -> bool:
        return path.suffix.lower() in self.config.text_extensions

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
        return {"ok": True, "query": query, "matches": matches, "truncated": len(matches) >= self.config.max_search_matches}

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
        file_path = self._resolve(path)
        if not file_path.exists():
            raise WorkspaceError(f"文件不存在: {_display_path(file_path, self.root)}")
        if not file_path.is_file():
            raise WorkspaceError(f"不是文件: {_display_path(file_path, self.root)}")
        if not self._is_text_file(file_path):
            raise WorkspaceError(f"第一阶段只支持文本文件: {file_path.suffix or '(无扩展名)'}")
        if start_line < 1 or (end_line is not None and end_line < start_line):
            raise WorkspaceError("行号范围无效。")
        lines = self._read_text(file_path).splitlines()
        selected_end = end_line or len(lines)
        selected = lines[start_line - 1 : selected_end]
        numbered = "\n".join(f"{number}: {line}" for number, line in enumerate(selected, start_line))
        truncated = len(numbered) > self.config.max_read_chars
        if truncated:
            numbered = numbered[: self.config.max_read_chars] + "\n[内容已截断]"
        return {
            "ok": True,
            "path": _display_path(file_path, self.root),
            "start_line": start_line,
            "end_line": min(selected_end, len(lines)),
            "content": numbered,
            "truncated": truncated,
        }


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "列出工作区中某个目录的直接内容。path 可使用相对工作区的路径，默认是工作区根目录。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "目录路径，默认 ."}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_file_content",
            "description": "在工作区的文本文件中按不区分大小写的字面字符串搜索，返回片段和行号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要搜索的字面字符串"},
                    "path": {"type": "string", "description": "搜索起点，默认 ."},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取工作区中文本文件的内容，可选地指定起止行号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "start_line": {"type": "integer", "minimum": 1, "description": "起始行号，默认 1"},
                    "end_line": {"type": "integer", "minimum": 1, "description": "结束行号，默认文件末尾"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
]


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
        reserve = config.max_output_tokens + math.ceil(window * config.context_safety_margin)
        available = min(window - reserve, config.model_max_input_tokens or window)
        if available <= 0:
            raise ConfigurationError(
                f"模型 {config.model} 的输出预留/安全余量过大；请调整 MAX_OUTPUT_TOKENS 和上下文参数。"
            )
        if config.context_keep_recent_tokens >= available:
            raise ConfigurationError(
                f"CONTEXT_KEEP_RECENT_TOKENS 必须小于可用输入预算 ({available})。"
            )
        if config.context_keep_recent_tokens + reserve >= window * config.context_compression_threshold:
            raise ConfigurationError(
                f"CONTEXT_KEEP_RECENT_TOKENS 过大，压缩后无法降到触发线以下；请调小该值或窗口参数。"
            )
        self.config = config
        return config

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        tool_choice: str = "auto",
    ) -> dict[str, Any]:
        self.last_usage = None
        window = self.config.context_window_tokens
        if window is not None:
            estimated = estimate_tokens(messages, tools)
            safe_input = window - self.config.max_output_tokens - math.ceil(window * self.config.context_safety_margin)
            if estimated > min(safe_input, self.config.model_max_input_tokens or safe_input):
                raise ModelRequestError("预计输入超过可用输入预算，压缩不足；请缩小本次输入或开启新会话。")
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": list(messages),
            "temperature": 0,
            "max_tokens": self.config.max_output_tokens,
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
    ):
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
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": self._system_prompt()}]
        self.context = ContextManager(self.config, recorder=self.store)
        self.tool_functions: dict[str, ToolFunction] = {
            "list_directory": self.workspace.list_directory,
            "search_file_content": self.workspace.search_file_content,
            "read_file": self.workspace.read_file,
        }
        self._restore_session()

    def _restore_session(self) -> None:
        """Load persisted history into this process, repairing interrupted rounds."""

        contents: SessionContents = self.store.load()
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
            payload = {"ok": False, "recovered": True, "error": "进程在工具执行前中断，该调用未执行。"}
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
            f"允许访问的工作区是 {self.config.root_dir}；文本扩展名包括 {extensions}。\n"
            "需要文件信息时先使用工具，不要凭空猜测。工具返回的失败不能证明内容不存在。"
            "回答时区分已确认的事实和不确定性，并使用用户的语言。"
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

    def _execute_tool(self, name: str, arguments: Any) -> dict[str, Any]:
        function = self.tool_functions.get(name)
        if function is None:
            return {"ok": False, "error": f"未知工具: {name}"}
        try:
            return function(**self._normalise_args(arguments))
        except (TypeError, WorkspaceError, OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

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
        self.context.begin_task(user_text)
        self.usage_ledger.reset()
        self._append_message({"role": "user", "content": user_text})
        active_calls: list[Mapping[str, Any]] = []
        handled_call_indexes: set[int] = set()
        try:
            for round_number in range(1, self.config.max_rounds + 1):
                final_round = round_number == self.config.max_rounds
                self.context.set_round(round_number)
                print(f"\n[第 {round_number}/{self.config.max_rounds} 轮] 请求模型" + ("（收尾）" if final_round else ""))
                tools = [] if final_round else TOOL_DEFINITIONS
                request_messages = self.context.prepare_messages(self.messages, tools, self.compression_client)
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
                        f"[上下文压缩] {event.method}: {event.before_tokens} -> {event.after_tokens} tokens"
                        + (f"；{event.warning}" if event.warning else "")
                    )
                if metrics.get("over_budget"):
                    raise ModelRequestError("压缩后输入仍超过可用输入预算；请缩小本次输入或开启新会话。")
                message = self.client.complete(
                    request_messages,
                    tools,
                    "none" if final_round else "auto",
                )
                self.context.record_usage(self.client.last_usage, request_messages, tools)
                assistant = self._assistant_message(message)
                self._append_message(assistant)
                calls = self._tool_calls(message)
                active_calls = calls
                handled_call_indexes = set()
                if not calls:
                    answer = message.get("content") or "模型没有返回文字回答。"
                    print(f"\nJarvis> {answer}")
                    return str(answer)
                if final_round:
                    print("已达到轮次上限，本次请求未完成。")
                    return None
                for call_index, call in enumerate(calls):
                    function_data = call.get("function", {}) if isinstance(call, Mapping) else {}
                    name = function_data.get("name", "")
                    arguments = function_data.get("arguments", {})
                    call_id = call.get("id", "")
                    print(f"[工具] {name} 参数: {arguments}")
                    try:
                        result = self._execute_tool(name, arguments)
                    except KeyboardInterrupt:
                        result = {"ok": False, "cancelled": True, "error": "工具调用已取消。"}
                        result_text = json.dumps(result, ensure_ascii=False)
                        print(f"[工具结果] {self._tool_result_for_display(name, result)}")
                        self._append_message({"role": "tool", "tool_call_id": call_id, "name": name, "content": result_text})
                        try:
                            normalised_arguments = self._normalise_args(arguments)
                        except WorkspaceError:
                            normalised_arguments = {}
                        self.context.record_tool_result(name, normalised_arguments, result, call_id)
                        handled_call_indexes.add(call_index)
                        for pending_call in calls[call_index + 1 :]:
                            pending_data = pending_call.get("function", {}) if isinstance(pending_call, Mapping) else {}
                            pending_name = pending_data.get("name", "")
                            pending_id = pending_call.get("id", "")
                            pending_result = json.dumps(
                                {"ok": False, "cancelled": True, "error": "工具调用因用户取消而未执行。"},
                                ensure_ascii=False,
                            )
                            self._append_message(
                                {"role": "tool", "tool_call_id": pending_id, "name": pending_name, "content": pending_result}
                            )
                            try:
                                pending_arguments = self._normalise_args(pending_data.get("arguments", {}))
                            except WorkspaceError:
                                pending_arguments = {}
                            self.context.record_tool_result(
                                pending_name,
                                pending_arguments,
                                {"ok": False, "cancelled": True, "error": "工具调用因用户取消而未执行。"},
                                pending_id,
                            )
                        active_calls = []
                        print("\n已取消当前请求。已完成的工具结果已保留，未完成的调用已标记为取消。")
                        return None
                    result_text = json.dumps(result, ensure_ascii=False)
                    print(f"[工具结果] {self._tool_result_for_display(name, result)}")
                    self._append_message({"role": "tool", "tool_call_id": call_id, "name": name, "content": result_text})
                    try:
                        normalised_arguments = self._normalise_args(arguments)
                    except WorkspaceError:
                        normalised_arguments = {}
                    self.context.record_tool_result(name, normalised_arguments, result, call_id)
                    handled_call_indexes.add(call_index)
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
            self.messages[:] = message_snapshot
            self.context.restore(context_snapshot)
            if self.store is not None and request_offset is not None:
                self.store.truncate_to(request_offset)
            print(f"模型请求失败: {exc}")
            return None
        finally:
            self.usage_ledger.summary()


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
        agent = Agent(config, resume=args.resume)
    except ConfigurationError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except (SessionNotFoundError, SessionLockedError) as exc:
        print(f"会话错误: {exc}", file=sys.stderr)
        return 2

    print("Jarvis 已启动。输入 exit 退出，Ctrl+C 取消当前请求。")
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
            agent.run_request(user_text)
    finally:
        agent.close()


if __name__ == "__main__":
    raise SystemExit(main())
