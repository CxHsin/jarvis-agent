from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class TelegramConfig(BaseModel):
    bot_token: str
    allowed_chat_ids: list[str] = Field(default_factory=list)


class LlmConfig(BaseModel):
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    model: str
    timeout_seconds: int = 60


class SearchConfig(BaseModel):
    provider: str = "duckduckgo"


class RuntimeConfig(BaseModel):
    workspace: str = "."
    data_dir: str = "data"
    log_dir: str = "logs"
    log_level: str = "INFO"
    session_history_limit: int = 12
    max_tool_round_trips: int = 8


class AppConfig(BaseModel):
    telegram: TelegramConfig
    llm: LlmConfig
    search: SearchConfig = Field(default_factory=SearchConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

    @property
    def workspace_path(self) -> Path:
        return Path(self.runtime.workspace).resolve()

    @property
    def data_dir_path(self) -> Path:
        return self.workspace_path / self.runtime.data_dir

    @property
    def log_dir_path(self) -> Path:
        return self.workspace_path / self.runtime.log_dir

    @property
    def session_store_path(self) -> Path:
        return self.data_dir_path / "sessions.json"


def load_config(path: str | Path = "config.toml") -> AppConfig:
    config_path = Path(path)
    raw = tomllib.loads(_resolve_env(config_path.read_text(encoding="utf-8")))
    return AppConfig.model_validate(raw)


def write_example_config(target: str | Path) -> None:
    source = Path("config.example.toml")
    Path(target).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _resolve_env(content: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), content)
