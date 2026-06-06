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
    poll_timeout_seconds: int
    request_timeout_seconds: int


DEFAULT_CONFIG_PATH = Path("config.toml")
DEFAULT_SYSTEM_PROMPT = "You are a concise, helpful personal AI assistant."


@dataclass(frozen=True)
class ConfigDraft:
    bot_token: str
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    system_prompt: str
    poll_timeout_seconds: int
    request_timeout_seconds: int


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
        poll_timeout_seconds=_read_int(config, "telegram.poll_timeout_seconds", 30),
        request_timeout_seconds=_read_int(config, "openai.request_timeout_seconds", 30),
    )


def _read_string(config: dict[str, object], dotted_key: str, default: str) -> str:
    value = _lookup_config_value(config, dotted_key)
    if isinstance(value, str) and value.strip():
        return value.strip()
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
        poll_timeout_seconds=draft.poll_timeout_seconds,
        request_timeout_seconds=draft.request_timeout_seconds,
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
