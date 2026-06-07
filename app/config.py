import json
import os
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path
import tomllib


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    bot_token: str
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    system_prompt: str
    conversation_max_rounds: int
    poll_timeout_seconds: int
    request_timeout_seconds: int
    memory_root_dir: Path
    tool_max_steps: int
    enabled_plugins: tuple[str, ...] = ()
    disabled_plugins: tuple[str, ...] = ()
    plugin_configs: dict[str, dict[str, object]] | None = None
    proactive_enabled: bool = False
    proactive_chat_id: int | None = None
    proactive_tick_interval_seconds: int = 300
    proactive_cooldown_seconds: int = 1800
    proactive_user_active_grace_seconds: int = 900
    proactive_candidate_limit: int = 3
    proactive_max_sends_per_tick: int = 1
    drift_enabled: bool = False
    drift_tick_interval_seconds: int = 300
    drift_idle_grace_seconds_after_user_message: int = 900
    drift_idle_grace_seconds_after_proactive_send: int = 900
    drift_dedupe_window_seconds: int = 3600
    drift_max_task_runtime_seconds: int = 30
    drift_max_task_cost: int = 3


DEFAULT_CONFIG_PATH = Path("config.toml")
DEFAULT_SYSTEM_PROMPT = "You are a concise, helpful personal AI assistant."


@dataclass(frozen=True)
class ConfigDraft:
    bot_token: str
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    system_prompt: str
    conversation_max_rounds: int
    poll_timeout_seconds: int
    request_timeout_seconds: int
    memory_root_dir: str
    tool_max_steps: int
    enabled_plugins: tuple[str, ...] = ()
    disabled_plugins: tuple[str, ...] = ()
    proactive_enabled: bool = False
    proactive_chat_id: str = ""
    proactive_tick_interval_seconds: int = 300
    proactive_cooldown_seconds: int = 1800
    proactive_user_active_grace_seconds: int = 900
    proactive_candidate_limit: int = 3
    proactive_max_sends_per_tick: int = 1
    drift_enabled: bool = False
    drift_tick_interval_seconds: int = 300
    drift_idle_grace_seconds_after_user_message: int = 900
    drift_idle_grace_seconds_after_proactive_send: int = 900
    drift_dedupe_window_seconds: int = 3600
    drift_max_task_runtime_seconds: int = 30
    drift_max_task_cost: int = 3


def _load_config_file(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Configuration root in {path} must be a table")
    return data


def _lookup_config_value(config: dict[str, object], dotted_key: str) -> object | None:
    current: object = config
    for key in dotted_key.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _draft_from_config(config: dict[str, object]) -> ConfigDraft:
    return ConfigDraft(
        bot_token=_read_string(config, "telegram.bot_token", ""),
        openai_api_key=_read_string(config, "openai.api_key", ""),
        openai_base_url=_read_string(config, "openai.base_url", "https://api.openai.com/v1"),
        openai_model=_read_string(config, "openai.model", "gpt-4o-mini"),
        system_prompt=_read_string(config, "agent.system_prompt", DEFAULT_SYSTEM_PROMPT),
        conversation_max_rounds=_read_int(config, "conversation.max_rounds", 6),
        poll_timeout_seconds=_read_int(config, "telegram.poll_timeout_seconds", 30),
        request_timeout_seconds=_read_int(config, "openai.request_timeout_seconds", 30),
        memory_root_dir=_read_string(config, "memory.root_dir", "memory"),
        tool_max_steps=_read_int(config, "tools.max_steps", 3),
        enabled_plugins=_read_string_list(config, "plugins.enabled", ()),
        disabled_plugins=_read_string_list(config, "plugins.disabled", ()),
        proactive_enabled=_read_bool(config, "proactive.enabled", False),
        proactive_chat_id=_read_string(config, "proactive.chat_id", ""),
        proactive_tick_interval_seconds=_read_int(config, "proactive.tick_interval_seconds", 300),
        proactive_cooldown_seconds=_read_int(config, "proactive.cooldown_seconds", 1800),
        proactive_user_active_grace_seconds=_read_int(
            config,
            "proactive.user_active_grace_seconds",
            900,
        ),
        proactive_candidate_limit=_read_int(config, "proactive.candidate_limit", 3),
        proactive_max_sends_per_tick=_read_int(config, "proactive.max_sends_per_tick", 1),
        drift_enabled=_read_bool(config, "drift.enabled", False),
        drift_tick_interval_seconds=_read_int(config, "drift.tick_interval_seconds", 300),
        drift_idle_grace_seconds_after_user_message=_read_int(
            config,
            "drift.idle_grace_seconds_after_user_message",
            900,
        ),
        drift_idle_grace_seconds_after_proactive_send=_read_int(
            config,
            "drift.idle_grace_seconds_after_proactive_send",
            900,
        ),
        drift_dedupe_window_seconds=_read_int(config, "drift.dedupe_window_seconds", 3600),
        drift_max_task_runtime_seconds=_read_int(config, "drift.max_task_runtime_seconds", 30),
        drift_max_task_cost=_read_int(config, "drift.max_task_cost", 3),
    )


def _read_plugin_configs(config: dict[str, object]) -> dict[str, dict[str, object]]:
    raw = _lookup_config_value(config, "plugin_config")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, object]] = {}
    for plugin_id, value in raw.items():
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            continue
        if not isinstance(value, dict):
            continue
        result[plugin_id.strip()] = dict(value)
    return result


def _read_string(config: dict[str, object], dotted_key: str, default: str) -> str:
    value = _lookup_config_value(config, dotted_key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _read_bool(config: dict[str, object], dotted_key: str, default: bool) -> bool:
    value = _lookup_config_value(config, dotted_key)
    if isinstance(value, bool):
        return value
    return default


def _read_int(config: dict[str, object], dotted_key: str, default: int) -> int:
    value = _lookup_config_value(config, dotted_key)
    if value is None:
        return default
    try:
        parsed = int(str(value).strip())
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _read_string_list(
    config: dict[str, object],
    dotted_key: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    value = _lookup_config_value(config, dotted_key)
    if not isinstance(value, list):
        return default
    items: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            items.append(item.strip())
    return tuple(items)


def _get_string(config: dict[str, object], env_name: str, dotted_key: str) -> str:
    env_value = os.getenv(env_name, "").strip()
    if env_value:
        return env_value

    file_value = _lookup_config_value(config, dotted_key)
    if isinstance(file_value, str) and file_value.strip():
        return file_value.strip()

    raise ConfigError(
        f"Missing required setting: {env_name} or '{dotted_key}' in {DEFAULT_CONFIG_PATH}"
    )


def _get_optional_string(
    config: dict[str, object],
    env_name: str,
    dotted_key: str,
    default: str,
) -> str:
    env_value = os.getenv(env_name, "").strip()
    if env_value:
        return env_value

    file_value = _lookup_config_value(config, dotted_key)
    if isinstance(file_value, str) and file_value.strip():
        return file_value.strip()
    return default


def _get_int(
    config: dict[str, object],
    env_name: str,
    dotted_key: str,
    default: int,
) -> int:
    env_value = os.getenv(env_name, "").strip()
    if env_value:
        raw = env_value
    else:
        file_value = _lookup_config_value(config, dotted_key)
        if file_value is None:
            raw = str(default)
        else:
            raw = str(file_value).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"Setting {env_name} or '{dotted_key}' in {DEFAULT_CONFIG_PATH} must be an integer"
        ) from exc
    if value <= 0:
        raise ConfigError(
            f"Setting {env_name} or '{dotted_key}' in {DEFAULT_CONFIG_PATH} must be greater than zero"
        )
    return value


def _get_optional_int(
    config: dict[str, object],
    env_name: str,
    dotted_key: str,
) -> int | None:
    env_value = os.getenv(env_name, "").strip()
    if env_value:
        raw = env_value
    else:
        file_value = _lookup_config_value(config, dotted_key)
        if file_value is None or (isinstance(file_value, str) and not file_value.strip()):
            return None
        raw = str(file_value).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"Setting {env_name} or '{dotted_key}' in {DEFAULT_CONFIG_PATH} must be an integer"
        ) from exc
    return value


def _get_bool(
    config: dict[str, object],
    env_name: str,
    dotted_key: str,
    default: bool,
) -> bool:
    env_value = os.getenv(env_name, "").strip().lower()
    if env_value:
        if env_value in {"1", "true", "yes", "on"}:
            return True
        if env_value in {"0", "false", "no", "off"}:
            return False
        raise ConfigError(
            f"Setting {env_name} or '{dotted_key}' in {DEFAULT_CONFIG_PATH} must be a boolean"
        )
    file_value = _lookup_config_value(config, dotted_key)
    if isinstance(file_value, bool):
        return file_value
    return default


def _get_string_list(
    config: dict[str, object],
    env_name: str,
    dotted_key: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    env_value = os.getenv(env_name, "").strip()
    if env_value:
        return tuple(part.strip() for part in env_value.split(",") if part.strip())

    value = _lookup_config_value(config, dotted_key)
    if not isinstance(value, list):
        return default

    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigError(
                f"Setting {env_name} or '{dotted_key}' in {DEFAULT_CONFIG_PATH} must be a string list"
            )
        stripped = item.strip()
        if stripped:
            items.append(stripped)
    return tuple(items)


def load_settings() -> Settings:
    config = _load_config_file(DEFAULT_CONFIG_PATH)
    return settings_from_config(config)


def settings_from_config(config: dict[str, object]) -> Settings:
    return Settings(
        bot_token=_get_string(config, "BOT_TOKEN", "telegram.bot_token"),
        openai_api_key=_get_string(config, "OPENAI_API_KEY", "openai.api_key"),
        openai_base_url=_get_string(config, "OPENAI_BASE_URL", "openai.base_url").rstrip("/"),
        openai_model=_get_string(config, "OPENAI_MODEL", "openai.model"),
        system_prompt=_get_optional_string(
            config,
            "SYSTEM_PROMPT",
            "agent.system_prompt",
            DEFAULT_SYSTEM_PROMPT,
        ),
        conversation_max_rounds=_get_int(
            config,
            "CONVERSATION_MAX_ROUNDS",
            "conversation.max_rounds",
            6,
        ),
        poll_timeout_seconds=_get_int(
            config,
            "POLL_TIMEOUT_SECONDS",
            "telegram.poll_timeout_seconds",
            30,
        ),
        request_timeout_seconds=_get_int(
            config,
            "REQUEST_TIMEOUT_SECONDS",
            "openai.request_timeout_seconds",
            30,
        ),
        memory_root_dir=Path(
            _get_optional_string(
                config,
                "MEMORY_ROOT_DIR",
                "memory.root_dir",
                "memory",
            )
        ),
        tool_max_steps=_get_int(
            config,
            "TOOL_MAX_STEPS",
            "tools.max_steps",
            3,
        ),
        enabled_plugins=_get_string_list(
            config,
            "ENABLED_PLUGINS",
            "plugins.enabled",
            (),
        ),
        disabled_plugins=_get_string_list(
            config,
            "DISABLED_PLUGINS",
            "plugins.disabled",
            (),
        ),
        plugin_configs=_read_plugin_configs(config),
        proactive_enabled=_get_bool(
            config,
            "PROACTIVE_ENABLED",
            "proactive.enabled",
            False,
        ),
        proactive_chat_id=_get_optional_int(
            config,
            "PROACTIVE_CHAT_ID",
            "proactive.chat_id",
        ),
        proactive_tick_interval_seconds=_get_int(
            config,
            "PROACTIVE_TICK_INTERVAL_SECONDS",
            "proactive.tick_interval_seconds",
            300,
        ),
        proactive_cooldown_seconds=_get_int(
            config,
            "PROACTIVE_COOLDOWN_SECONDS",
            "proactive.cooldown_seconds",
            1800,
        ),
        proactive_user_active_grace_seconds=_get_int(
            config,
            "PROACTIVE_USER_ACTIVE_GRACE_SECONDS",
            "proactive.user_active_grace_seconds",
            900,
        ),
        proactive_candidate_limit=_get_int(
            config,
            "PROACTIVE_CANDIDATE_LIMIT",
            "proactive.candidate_limit",
            3,
        ),
        proactive_max_sends_per_tick=_get_int(
            config,
            "PROACTIVE_MAX_SENDS_PER_TICK",
            "proactive.max_sends_per_tick",
            1,
        ),
        drift_enabled=_get_bool(
            config,
            "DRIFT_ENABLED",
            "drift.enabled",
            False,
        ),
        drift_tick_interval_seconds=_get_int(
            config,
            "DRIFT_TICK_INTERVAL_SECONDS",
            "drift.tick_interval_seconds",
            300,
        ),
        drift_idle_grace_seconds_after_user_message=_get_int(
            config,
            "DRIFT_IDLE_GRACE_SECONDS_AFTER_USER_MESSAGE",
            "drift.idle_grace_seconds_after_user_message",
            900,
        ),
        drift_idle_grace_seconds_after_proactive_send=_get_int(
            config,
            "DRIFT_IDLE_GRACE_SECONDS_AFTER_PROACTIVE_SEND",
            "drift.idle_grace_seconds_after_proactive_send",
            900,
        ),
        drift_dedupe_window_seconds=_get_int(
            config,
            "DRIFT_DEDUPE_WINDOW_SECONDS",
            "drift.dedupe_window_seconds",
            3600,
        ),
        drift_max_task_runtime_seconds=_get_int(
            config,
            "DRIFT_MAX_TASK_RUNTIME_SECONDS",
            "drift.max_task_runtime_seconds",
            30,
        ),
        drift_max_task_cost=_get_int(
            config,
            "DRIFT_MAX_TASK_COST",
            "drift.max_task_cost",
            3,
        ),
    )


def run_setup_wizard(*, overwrite: bool, path: Path = DEFAULT_CONFIG_PATH) -> Path:
    if path.exists() and not overwrite:
        raise ConfigError(f"{path} 已存在。若要修改配置，请使用 init。")

    existing_config = _load_config_file(path)
    draft = _draft_from_config(existing_config)

    print("Personal Agent 配置向导")
    print("")
    print("请根据提示填写配置。若方括号里有默认值，直接回车即可保留。")
    print("")

    updated = ConfigDraft(
        bot_token=_prompt_secret(
            "Telegram 机器人 Token",
            draft.bot_token,
            "在 Telegram 的 BotFather 创建机器人后可以拿到这个值。",
        ),
        openai_api_key=_prompt_secret(
            "OpenAI 兼容接口 API Key",
            draft.openai_api_key,
            "填写你的模型服务商提供的 API Key。",
        ),
        openai_base_url=_prompt_required(
            "OpenAI 兼容接口 Base URL",
            draft.openai_base_url,
            "例如: https://api.openai.com/v1",
        ),
        openai_model=_prompt_required(
            "模型名称",
            draft.openai_model,
            "例如: gpt-4o-mini",
        ),
        system_prompt=draft.system_prompt,
        conversation_max_rounds=draft.conversation_max_rounds,
        poll_timeout_seconds=draft.poll_timeout_seconds,
        request_timeout_seconds=draft.request_timeout_seconds,
        memory_root_dir=draft.memory_root_dir,
        tool_max_steps=draft.tool_max_steps,
        enabled_plugins=draft.enabled_plugins,
        disabled_plugins=draft.disabled_plugins,
        proactive_enabled=draft.proactive_enabled,
        proactive_chat_id=draft.proactive_chat_id,
        proactive_tick_interval_seconds=draft.proactive_tick_interval_seconds,
        proactive_cooldown_seconds=draft.proactive_cooldown_seconds,
        proactive_user_active_grace_seconds=draft.proactive_user_active_grace_seconds,
        proactive_candidate_limit=draft.proactive_candidate_limit,
        proactive_max_sends_per_tick=draft.proactive_max_sends_per_tick,
        drift_enabled=draft.drift_enabled,
        drift_tick_interval_seconds=draft.drift_tick_interval_seconds,
        drift_idle_grace_seconds_after_user_message=(
            draft.drift_idle_grace_seconds_after_user_message
        ),
        drift_idle_grace_seconds_after_proactive_send=(
            draft.drift_idle_grace_seconds_after_proactive_send
        ),
        drift_dedupe_window_seconds=draft.drift_dedupe_window_seconds,
        drift_max_task_runtime_seconds=draft.drift_max_task_runtime_seconds,
        drift_max_task_cost=draft.drift_max_task_cost,
    )

    write_config_file(updated, path=path)
    return path


def write_config_file(draft: ConfigDraft, *, path: Path = DEFAULT_CONFIG_PATH) -> None:
    content = "\n".join(
        [
            "[telegram]",
            f"bot_token = {_toml_string(draft.bot_token)}",
            f"poll_timeout_seconds = {draft.poll_timeout_seconds}",
            "",
            "[openai]",
            f"api_key = {_toml_string(draft.openai_api_key)}",
            f"base_url = {_toml_string(draft.openai_base_url)}",
            f"model = {_toml_string(draft.openai_model)}",
            f"request_timeout_seconds = {draft.request_timeout_seconds}",
            "",
            "[agent]",
            f"system_prompt = {_toml_string(draft.system_prompt)}",
            "",
            "[conversation]",
            f"max_rounds = {draft.conversation_max_rounds}",
            "",
            "[memory]",
            f"root_dir = {_toml_string(draft.memory_root_dir)}",
            "",
            "[tools]",
            f"max_steps = {draft.tool_max_steps}",
            "",
            "[plugins]",
            f"enabled = {_toml_list(draft.enabled_plugins)}",
            f"disabled = {_toml_list(draft.disabled_plugins)}",
            "",
            "[proactive]",
            f"enabled = {_toml_bool(draft.proactive_enabled)}",
            f"chat_id = {_toml_string(draft.proactive_chat_id)}",
            f"tick_interval_seconds = {draft.proactive_tick_interval_seconds}",
            f"cooldown_seconds = {draft.proactive_cooldown_seconds}",
            f"user_active_grace_seconds = {draft.proactive_user_active_grace_seconds}",
            f"candidate_limit = {draft.proactive_candidate_limit}",
            f"max_sends_per_tick = {draft.proactive_max_sends_per_tick}",
            "",
            "[drift]",
            f"enabled = {_toml_bool(draft.drift_enabled)}",
            f"tick_interval_seconds = {draft.drift_tick_interval_seconds}",
            "idle_grace_seconds_after_user_message = "
            f"{draft.drift_idle_grace_seconds_after_user_message}",
            "idle_grace_seconds_after_proactive_send = "
            f"{draft.drift_idle_grace_seconds_after_proactive_send}",
            f"dedupe_window_seconds = {draft.drift_dedupe_window_seconds}",
            f"max_task_runtime_seconds = {draft.drift_max_task_runtime_seconds}",
            f"max_task_cost = {draft.drift_max_task_cost}",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")


def _prompt_required(label: str, current_value: str, help_text: str) -> str:
    while True:
        print(label)
        print(f"  {help_text}")
        suffix = f" [{current_value}]" if current_value else ""
        value = input(f"> {label}{suffix}: ").strip()
        if value:
            return value
        if current_value:
            return current_value
        print("  这是必填项。")
        print("")


def _prompt_secret(label: str, current_value: str, help_text: str) -> str:
    while True:
        print(label)
        print(f"  {help_text}")
        suffix = " [已保存，直接回车可保留]" if current_value else ""
        value = getpass(f"> {label}{suffix}: ").strip()
        if value:
            return value
        if current_value:
            return current_value
        print("  这是必填项。")
        print("")


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_list(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"
