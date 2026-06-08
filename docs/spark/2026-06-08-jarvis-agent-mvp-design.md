# Jarvis Agent MVP Design

## Overview

This spec defines the first runnable version of `jarvis-agent`.

The goal of v1 is a personal Telegram-based agent that can:

- receive Telegram messages
- reply with LLM-generated text
- execute local tools
- use basic web tools

This version explicitly prioritizes "can run end-to-end" over safety hardening, memory depth, or multi-channel support.

## Scope

### In Scope

- Single channel: Telegram
- Single primary user workflow
- Chat-based interaction
- Local tool execution
- Basic web search and page fetch
- Local session context for recent conversation continuity
- Local config, logs, and startup commands

### Out of Scope

- QQ, QQBot, and other channels
- Long-term memory and retrieval
- Proactive messaging
- Scheduling, drift tasks, and background autonomy
- Plugin system
- Multi-user authorization model
- Fine-grained sandboxing or security controls

## Product Goal

The first version is considered successful when the agent can run locally, connect to Telegram, answer messages, call local and web tools on demand, and return usable results back to the chat.

## Recommended Approach

Three implementation directions were considered:

1. Thin-core MVP
2. Medium layered architecture
3. Close-to-akashic reduced clone

The recommended direction is `Thin-core MVP`.

Reasoning:

- it minimizes time to first runnable version
- it keeps the code small enough to debug quickly
- it avoids copying the full weight of `akashic-agent`
- it still leaves room to evolve into clearer layers later

## Architecture

The v1 message path is:

`Telegram -> message handler -> agent loop -> LLM -> tool execution -> final reply`

The system should be organized into these modules:

### `app/main.py`

Responsibilities:

- load config
- initialize runtime dependencies
- start Telegram bot
- wire LLM client and tool registry
- expose simple CLI commands

### `jarvis/channels/telegram.py`

Responsibilities:

- receive Telegram updates
- normalize Telegram messages into internal input objects
- send final responses back to Telegram

This module is the only Telegram-specific runtime surface in v1.

### `jarvis/core/agent.py`

Responsibilities:

- orchestrate one chat turn
- assemble prompt context
- call the LLM
- detect or parse tool calls
- execute tools through a central runtime
- request the final answer after tool results are available

This is the main coordination layer for the agent.

### `jarvis/tools/`

Responsibilities:

- define built-in tools
- validate inputs
- execute tool logic
- standardize tool results

Planned built-in tools:

- `shell_exec`
- `read_file`
- `write_file`
- `web_search`
- `fetch_url`

### `jarvis/providers/llm.py`

Responsibilities:

- wrap one LLM provider for v1
- send chat requests
- return model outputs in a stable internal format

This layer should hide provider-specific request and response shapes from the rest of the code.

### `jarvis/state/session_store.py`

Responsibilities:

- store recent conversation history per Telegram chat
- load the recent message window for a new turn
- trim older entries

This is session continuity only, not a memory system.

## Tool Execution Model

The v1 execution model is a simple two-stage loop:

1. receive a user message and recent chat context
2. ask the LLM whether to answer directly or call a tool
3. if a tool is requested, validate and execute it
4. append the tool result to the current turn context
5. ask the LLM for the final user-facing reply

The implementation should keep all tool execution behind a single runtime boundary so the agent has one place to:

- resolve tools by name
- validate parameters
- capture timing
- capture success and failure
- support future permission controls

## Tool Set

### `shell_exec`

Purpose:

- run local shell commands

Expected return:

- stdout
- stderr
- exit code

### `read_file`

Purpose:

- read text files from the local workspace

Expected return:

- file content
- path metadata if needed

### `write_file`

Purpose:

- write or overwrite text files in the local workspace

Expected return:

- success/failure status
- target path

### `web_search`

Purpose:

- run a web search through a configured provider

Expected return:

- result titles
- short snippets
- URLs

### `fetch_url`

Purpose:

- fetch page content for a given URL

Expected return:

- extracted page text or raw HTML
- fetch status

## Safety Position for V1

The accepted v1 safety posture is local full access with minimal restrictions.

That means:

- no strong command allowlist in v1
- no strict filesystem sandbox in v1
- no production-grade user permission model in v1

However, the architecture should still preserve two basic control points:

- all tools must execute through a central `ToolRuntime`
- every tool call must be logged

This keeps the implementation lightweight while avoiding a rewrite when safety controls are added later.

## Conversation State

V1 does not include long-term memory.

Instead, it stores a small rolling message window per Telegram chat, sufficient for:

- keeping the current conversation coherent
- retaining recent tool outputs
- supporting short follow-up questions

Recommended storage options:

- `data/sessions.json`
- or `data/sessions.db`

The store should retain the latest `10-20` messages per chat and trim the rest.

## Configuration

V1 should use a single local config file, recommended as `config.toml`.

Minimum config fields:

- `telegram.bot_token`
- `telegram.allowed_chat_ids` or equivalent primary-user identifier
- `llm.provider`
- `llm.api_key`
- `llm.base_url`
- `llm.model`
- `search.provider`
- `search.api_key` or equivalent search config
- `runtime.workspace`
- `runtime.log_level`

## Startup Commands

The runtime interface should stay minimal:

- `python -m app.main init`
- `python -m app.main run`
- optional `python -m app.main doctor`

### `init`

Creates:

- default config template
- local data directory
- local log directory

### `run`

Starts:

- Telegram bot runtime
- LLM client
- tool registry
- session store

### `doctor`

Checks:

- config presence
- required keys
- workspace and data paths
- Telegram connectivity
- model provider connectivity

## Error Handling

V1 only needs visible, debuggable handling for three failure categories.

### LLM Failure

Behavior:

- return a short user-visible failure message
- write details to logs

### Tool Failure

Behavior:

- return a compressed explanation of the failure to the user
- write details to logs

### Telegram Transport Failure

Behavior:

- retry when appropriate
- otherwise record the failure clearly in logs

## Logging

Logs should be written locally, recommended path:

- `logs/jarvis.log`

Each turn should record enough information to debug behavior:

- timestamp
- Telegram chat id
- user message summary
- tool name if called
- duration
- success/failure
- error message when relevant

## Acceptance Criteria

V1 is complete when all of the following are true:

1. Starting the runtime locally brings the Telegram bot online.
2. Sending a normal Telegram message returns an LLM-generated reply.
3. Asking for a local command can trigger `shell_exec` and return the result.
4. Asking for a search or page fetch can trigger `web_search` or `fetch_url` and return a usable summary.
5. Model and tool failures produce understandable Telegram responses and leave debuggable local logs.

## Evolution Path After V1

The next likely steps after this MVP are:

- extract a stable channel abstraction so QQ and QQBot can be added
- split the thin core into clearer runtime layers
- add permission controls for tools
- add richer session storage or memory
- add plugins and proactive behaviors only after the passive path is stable

## Non-Goals for This Spec

This spec does not define:

- the exact Telegram library choice
- the exact LLM provider choice
- the exact search provider choice
- the final prompt wording

Those can be decided during implementation as long as they preserve the architecture and acceptance criteria above.
