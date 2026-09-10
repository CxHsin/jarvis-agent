"""Stage-one command-line personal agent.

The implementation deliberately keeps the agent loop visible: a model response
may request tools, each tool call is executed in order, and the results are
returned to the model until it answers or the round limit is reached.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import quote
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from context_manager import ContextManager


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
    text_extensions: tuple[str, ...] = DEFAULT_TEXT_EXTENSIONS
    request_timeout: float = 60.0
    max_read_chars: int = 12_000
    max_search_matches: int = 20
    compression_model: str | None = None
    context_window_tokens: int | None = None
    context_window_source: str = "unknown"
    context_compression_threshold: float = 0.86
    context_compression_target: float = 0.65
    context_summary_max_chars: int = 6_000
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

        context_threshold = ratio("CONTEXT_COMPRESSION_THRESHOLD", 0.86)
        context_target = ratio("CONTEXT_COMPRESSION_TARGET", 0.65)
        if context_target >= context_threshold:
            raise ConfigurationError("CONTEXT_COMPRESSION_TARGET 必须小于 CONTEXT_COMPRESSION_THRESHOLD。")

        context_window = optional_positive_int("CONTEXT_WINDOW_TOKENS")
        return cls(
            base_url=base_url.rstrip("/"),
            api_key=_setting(values, "API_KEY", "") or "",
            model=model,
            max_rounds=positive_int("MAX_ROUNDS", 5),
            root_dir=root_dir,
            text_extensions=extensions,
            request_timeout=timeout,
            max_read_chars=positive_int("MAX_READ_CHARS", 12_000),
            max_search_matches=positive_int("MAX_SEARCH_MATCHES", 20),
            compression_model=_setting(values, "COMPRESSION_MODEL") or None,
            context_window_tokens=context_window,
            context_window_source="configured" if context_window is not None else "unknown",
            context_compression_threshold=context_threshold,
            context_compression_target=context_target,
            context_summary_max_chars=positive_int("CONTEXT_SUMMARY_MAX_CHARS", 6_000),
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
        "max_input_tokens",
        "input_token_limit",
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
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    continue
                if parsed > 0:
                    return parsed
            for value in payload.values():
                found = cls._extract_context_window(value)
                if found is not None:
                    return found
        elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
            for value in payload:
                found = cls._extract_context_window(value)
                if found is not None:
                    return found
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
            discovered = self._extract_context_window(payload)
            if discovered is not None:
                return discovered
        return None

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        tool_choice: str = "auto",
    ) -> dict[str, Any]:
        self.last_usage = None
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": list(messages),
            "temperature": 0,
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
    ):
        self.client = client or ChatCompletionsClient(config)
        self.config = config
        if self.config.context_window_tokens is None and isinstance(self.client, ChatCompletionsClient):
            discovered_window = self.client.discover_context_window()
            if discovered_window is not None:
                self.config = replace(
                    self.config,
                    context_window_tokens=discovered_window,
                    context_window_source="upstream",
                )
                print(f"[上下文] 已从上游模型元数据获取窗口: {discovered_window} tokens")
        self.workspace = Workspace(self.config)
        if compression_client is not None:
            self.compression_client = compression_client
        elif self.config.compression_model and self.config.compression_model != self.config.model and isinstance(self.client, ChatCompletionsClient):
            self.compression_client = ChatCompletionsClient(replace(self.config, model=self.config.compression_model))
        else:
            self.compression_client = self.client
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": self._system_prompt()}]
        self.context = ContextManager(self.config)
        self.tool_functions: dict[str, ToolFunction] = {
            "list_directory": self.workspace.list_directory,
            "search_file_content": self.workspace.search_file_content,
            "read_file": self.workspace.read_file,
        }

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
        if message.get("tool_calls"):
            result["tool_calls"] = message["tool_calls"]
        return result

    def run_request(self, user_text: str) -> str | None:
        history_start = len(self.messages)
        self.context.begin_task(user_text)
        self.messages.append({"role": "user", "content": user_text})
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
                message = self.client.complete(
                    request_messages,
                    tools,
                    "none" if final_round else "auto",
                )
                self.context.record_usage(getattr(self.client, "last_usage", None))
                assistant = self._assistant_message(message)
                self.messages.append(assistant)
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
                        self.messages.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": result_text})
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
                            self.messages.append(
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
                    self.messages.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": result_text})
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
                self.messages.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": result_text})
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
            del self.messages[history_start:]
            print(f"模型请求失败: {exc}")
            return None


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Jarvis 第一阶段命令行 Agent")
    parser.add_argument("--env-file", type=Path, default=Path.cwd() / ".env", help="配置文件路径")
    args = parser.parse_args(argv)
    try:
        config = Config.from_env(args.env_file)
    except ConfigurationError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    agent = Agent(config)
    print("Jarvis 已启动。输入 exit 退出，Ctrl+C 取消当前请求。")
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


if __name__ == "__main__":
    raise SystemExit(main())
