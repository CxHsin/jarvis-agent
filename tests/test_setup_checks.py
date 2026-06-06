from pathlib import Path

from app.config import Settings
from app.setup_checks import verify_openai_compatible, verify_telegram_token
import pytest


class StubResponse:
    def __init__(self, payload: dict, status_ok: bool = True) -> None:
        self._payload = payload
        self._status_ok = status_ok

    def raise_for_status(self) -> None:
        if not self._status_ok:
            raise RuntimeError("bad status")

    def json(self) -> dict:
        return self._payload


class StubLLMClient:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def chat(self, messages):  # noqa: ANN001
        return self.reply


def build_settings() -> Settings:
    return Settings(
        bot_token="bot-token",
        openai_api_key="api-key",
        openai_base_url="https://example.com/v1",
        openai_model="gpt-test",
        system_prompt="prompt",
        conversation_max_rounds=6,
        poll_timeout_seconds=30,
        request_timeout_seconds=30,
        memory_root_dir=Path("memory"),
    )


def test_verify_telegram_token_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.setup_checks as setup_checks

    monkeypatch.setattr(
        setup_checks.requests,
        "get",
        lambda *args, **kwargs: StubResponse({"ok": True, "result": {"username": "jarvis_bot"}}),
    )

    ok, message = verify_telegram_token(build_settings())

    assert ok is True
    assert "jarvis_bot" in message


def test_verify_openai_compatible_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.setup_checks as setup_checks

    monkeypatch.setattr(
        setup_checks,
        "OpenAICompatibleClient",
        lambda **kwargs: StubLLMClient("OK"),
    )

    ok, message = verify_openai_compatible(build_settings())

    assert ok is True
    assert "OK" in message
