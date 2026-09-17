from types import SimpleNamespace

import pytest

from jarvis_agent import Agent, Config, DEFAULT_SYSTEM_PROMPT


@pytest.mark.parametrize('value', ['请用中文简洁回答。', '', '   '])
def test_system_prompt_from_dotenv(tmp_path, monkeypatch, value):
    monkeypatch.delenv('SYSTEM_PROMPT', raising=False)
    monkeypatch.setenv('STATE_DIR', str(tmp_path / 'state'))
    path = tmp_path / '.env'
    path.write_text(f'BASE_URL=http://example.test\nMODEL=test\nSYSTEM_PROMPT="{value}"\n', encoding='utf-8')
    config = Config.from_env(path)
    assert config.system_prompt == (value.strip() or DEFAULT_SYSTEM_PROMPT)
    prompt = Agent._system_prompt(SimpleNamespace(config=config))
    assert prompt.startswith(config.system_prompt)
    assert 'tool_search' in prompt
    assert str(config.root_dir) in prompt
    if value.strip():
        assert '你是 Jarvis' not in prompt


def test_environment_overrides_dotenv_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv('SYSTEM_PROMPT', '环境指定提示词')
    monkeypatch.setenv('STATE_DIR', str(tmp_path / 'state'))
    path = tmp_path / '.env'
    path.write_text('BASE_URL=http://example.test\nMODEL=test\nSYSTEM_PROMPT=文件提示词\n', encoding='utf-8')
    assert Config.from_env(path).system_prompt == '环境指定提示词'
