"""OpenAI-compatible model transport and capacity resolution."""
import json
import urllib.error
import urllib.request
from urllib.parse import quote
from dataclasses import replace
from typing import Any, Mapping, Sequence
from configuration import Config, ConfigurationError
from context_budget import ContextBudget
from context_manager import estimate_tokens
from model_capabilities import load_capability

class ModelRequestError(RuntimeError):
    """Raised when the model endpoint cannot complete a request."""


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
