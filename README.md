# Jarvis Agent

Telegram-first personal agent MVP.

## What V1 Does

- receives Telegram messages
- replies with an LLM
- can execute local shell commands
- can read and write local files
- can search the web
- can fetch URL content

## Setup

Use a standard Python installation with `pip`. On this machine, `py -3.12` works.

```bash
py -3.12 -m pip install -r requirements.txt
py -3.12 -m app.main init
```

Then edit `config.toml`:

- set `telegram.bot_token`
- optionally set `telegram.allowed_chat_ids`
- set `llm.api_key`
- set `llm.base_url`
- set `llm.model`

The LLM config follows the same OpenAI-compatible pattern used by `akashic-agent`.

Examples:

```toml
[llm]
api_key = "${OPENAI_API_KEY}"
base_url = "https://api.openai.com/v1"
model = "gpt-4.1-mini"
```

```toml
[llm]
api_key = "${DEEPSEEK_API_KEY}"
base_url = "https://api.deepseek.com/v1"
model = "deepseek-chat"
```

```toml
[llm]
api_key = "${DASHSCOPE_API_KEY}"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
model = "qwen-plus"
```

## Commands

```bash
py -3.12 -m app.main init
py -3.12 -m app.main doctor
py -3.12 -m app.main run
```

## Notes

- session state is stored in `data/sessions.json`
- logs are written to `logs/jarvis.log`
- tools currently run with local full access
