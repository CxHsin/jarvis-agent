"""Configuration loading and persisted user defaults."""
import json
import os
from pathlib import Path
from dataclasses import dataclass, field, replace
from typing import Mapping
from session.session_store import resolve_state_dir
from tools.tool_runtime import PermissionPolicy

DEFAULT_TEXT_EXTENSIONS = (".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".py")

DEFAULT_SYSTEM_PROMPT = (
    "你是 Jarvis，一个通用的个人助理。当前阶段可以使用文件工具完成用户请求。\n"
    "需要文件信息时先使用工具，不要凭空猜测。工具返回的失败不能证明内容不存在。"
    "回答时区分已确认的事实和不确定性，并使用用户的语言。"
)


class ConfigurationError(ValueError):
    """Raised when the local configuration cannot start the agent."""


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


GLOBAL_SETTINGS_FILE = "settings.json"


def _global_settings_path(state_dir: Path) -> Path:
    return state_dir / GLOBAL_SETTINGS_FILE


def load_global_tool_permission_mode(state_dir: Path) -> str | None:
    """Read the user-wide permission default, if one was explicitly saved."""

    path = _global_settings_path(state_dir)
    if not path.exists():
        return None
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"无法读取全局设置: {path}") from exc
    if not isinstance(values, dict):
        raise ConfigurationError("全局设置必须是 JSON 对象。")
    mode = values.get("tool_permission_mode")
    if mode is not None and mode not in PermissionPolicy.MODES:
        raise ConfigurationError("全局设置中的 TOOL_PERMISSION_MODE 无效。")
    return mode


def save_global_tool_permission_mode(state_dir: Path, mode: str) -> None:
    """Atomically save the user-wide tool permission default."""

    if mode not in PermissionPolicy.MODES:
        raise ValueError("invalid permission mode")
    path = _global_settings_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"tool_permission_mode": mode}, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


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
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
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
        configured_permission_mode = _setting(values, "TOOL_PERMISSION_MODE")
        state_config = cls(
            base_url=base_url.rstrip("/"),
            api_key=_setting(values, "API_KEY", "") or "",
            model=model,
            max_rounds=positive_int("MAX_ROUNDS", 5),
            root_dir=root_dir,
            state_dir=state_dir,
            tool_permission_mode=configured_permission_mode or "approve-dangerous",
            system_prompt=(_setting(values, "SYSTEM_PROMPT") or "").strip() or DEFAULT_SYSTEM_PROMPT,
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
        if os.environ.get("TOOL_PERMISSION_MODE") is not None:
            return state_config
        global_permission_mode = load_global_tool_permission_mode(resolve_state_dir(state_config))
        return replace(state_config, tool_permission_mode=global_permission_mode or state_config.tool_permission_mode)
