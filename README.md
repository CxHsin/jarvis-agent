# Personal Agent

Phase 1 implementation of a personal Telegram agent based on `docs/spark/2026-06-06-personal-agent-phase1-design.md`.

## Setup

```powershell
python -m venv .venv
& '.\.venv\bin\python.exe' -m pip install -r requirements.txt
```

## Configuration

First-time setup:

```powershell
& '.\.venv\bin\python.exe' -m app.main setup
```

`setup` uses Chinese prompts and asks only for:

- Telegram bot token
- OpenAI-compatible API key
- Base URL
- model name

Sensitive values are hidden while typing. If a token or key is already saved, the wizard will not show the old value in plaintext; it only shows that a value is already saved and lets you press Enter to keep it.

After writing `config.toml`, the wizard checks:

- whether the Telegram token is valid
- whether the OpenAI-compatible API is reachable

To update an existing config:

```powershell
& '.\.venv\bin\python.exe' -m app.main init
```

System-level settings such as `system_prompt`, polling timeout, and request timeout are written with defaults and are not asked in the wizard. Edit `config.toml` manually if you need to change them.

## Run

```powershell
& '.\.venv\bin\python.exe' -m app.main
```

## Test

```powershell
& '.\.venv\bin\python.exe' -m pytest
```
