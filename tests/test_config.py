from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import ConfigDraft, ConfigError, load_settings, run_setup_wizard, write_config_file


def test_load_settings_reads_required_values_from_toml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[telegram]",
                'bot_token = "bot-token"',
                "poll_timeout_seconds = 10",
                "",
                "[openai]",
                'api_key = "api-key"',
                'base_url = "https://example.com/v1/"',
                'model = "gpt-test"',
                "request_timeout_seconds = 40",
                "",
                "[agent]",
                'system_prompt = "system prompt"',
                "",
                "[conversation]",
                "max_rounds = 8",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    settings = load_settings()

    assert settings.bot_token == "bot-token"
    assert settings.openai_api_key == "api-key"
    assert settings.openai_base_url == "https://example.com/v1"
    assert settings.openai_model == "gpt-test"
    assert settings.system_prompt == "system prompt"
    assert settings.conversation_max_rounds == 8
    assert settings.poll_timeout_seconds == 10
    assert settings.request_timeout_seconds == 40


def test_load_settings_allows_environment_variables_to_override_file_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[telegram]",
                'bot_token = "file-token"',
                "",
                "[openai]",
                'api_key = "file-key"',
                'base_url = "https://file.example/v1"',
                'model = "file-model"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_TOKEN", "env-token")
    monkeypatch.setenv("OPENAI_MODEL", "env-model")

    settings = load_settings()

    assert settings.bot_token == "env-token"
    assert settings.openai_api_key == "file-key"
    assert settings.openai_model == "env-model"
    assert settings.conversation_max_rounds == 6


def test_load_settings_fails_when_required_values_are_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="BOT_TOKEN|telegram\\.bot_token"):
        load_settings()


def test_load_settings_fails_when_toml_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[telegram\nbot_token = 'oops'", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="Invalid TOML"):
        load_settings()


def test_load_settings_fails_when_timeout_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[telegram]",
                'bot_token = "bot-token"',
                'poll_timeout_seconds = "zero"',
                "",
                "[openai]",
                'api_key = "api-key"',
                'base_url = "https://example.com/v1"',
                'model = "gpt-test"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="POLL_TIMEOUT_SECONDS"):
        load_settings()


def test_write_config_file_outputs_expected_toml(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    draft = ConfigDraft(
        bot_token="bot-token",
        openai_api_key="api-key",
        openai_base_url="https://example.com/v1",
        openai_model="gpt-test",
        system_prompt="system prompt",
        conversation_max_rounds=7,
        poll_timeout_seconds=12,
        request_timeout_seconds=34,
    )

    write_config_file(draft, path=target)

    content = target.read_text(encoding="utf-8")
    assert 'bot_token = "bot-token"' in content
    assert 'api_key = "api-key"' in content
    assert "poll_timeout_seconds = 12" in content
    assert 'system_prompt = "system prompt"' in content
    assert "max_rounds = 7" in content


def test_setup_wizard_creates_config_file_from_user_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    secret_answers = iter(
        [
            "telegram-token",
            "api-key",
        ]
    )
    normal_answers = iter(
        [
            "https://example.com/v1",
            "gpt-test",
        ]
    )

    with patch("app.config.getpass", side_effect=lambda _: next(secret_answers)):
        with patch("builtins.input", side_effect=lambda _: next(normal_answers)):
            path = run_setup_wizard(overwrite=False)

    assert path == Path("config.toml")
    content = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert 'bot_token = "telegram-token"' in content
    assert 'api_key = "api-key"' in content
    assert 'model = "gpt-test"' in content
    assert 'system_prompt = "You are a concise, helpful personal AI assistant."' in content
    assert "max_rounds = 6" in content
    assert "poll_timeout_seconds = 30" in content
    assert "request_timeout_seconds = 30" in content


def test_setup_wizard_keeps_existing_secret_when_user_presses_enter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        "\n".join(
            [
                "[telegram]",
                'bot_token = "saved-token"',
                "",
                "[openai]",
                'api_key = "saved-key"',
                'base_url = "https://example.com/v1"',
                'model = "gpt-test"',
            ]
        ),
        encoding="utf-8",
    )
    secret_answers = iter(["", ""])
    normal_answers = iter(["", ""])

    with patch("app.config.getpass", side_effect=lambda _: next(secret_answers)):
        with patch("builtins.input", side_effect=lambda _: next(normal_answers)):
            path = run_setup_wizard(overwrite=True)

    assert path == Path("config.toml")
    content = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert 'bot_token = "saved-token"' in content
    assert 'api_key = "saved-key"' in content


def test_setup_wizard_does_not_show_existing_secret_values_in_prompts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        "\n".join(
            [
                "[telegram]",
                'bot_token = "saved-token"',
                "",
                "[openai]",
                'api_key = "saved-key"',
                'base_url = "https://example.com/v1"',
                'model = "gpt-test"',
            ]
        ),
        encoding="utf-8",
    )
    prompts: list[str] = []
    secret_answers = iter(["", ""])
    normal_answers = iter(["", ""])

    def capture_getpass(prompt: str) -> str:
        prompts.append(prompt)
        return next(secret_answers)

    with patch("app.config.getpass", side_effect=capture_getpass):
        with patch("builtins.input", side_effect=lambda _: next(normal_answers)):
            path = run_setup_wizard(overwrite=True)

    assert path == Path("config.toml")
    assert all("saved-token" not in prompt for prompt in prompts)
    assert all("saved-key" not in prompt for prompt in prompts)
    assert any("已保存" in prompt for prompt in prompts)


def test_setup_wizard_rejects_existing_config_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text("[telegram]\nbot_token = \"x\"\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="init"):
        run_setup_wizard(overwrite=False)
