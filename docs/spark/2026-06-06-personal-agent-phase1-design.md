# Personal Agent Phase 1 Design

## Goal

Build a personal-use agent from zero in a way that maximizes understanding of why each architectural choice exists.

This spec defines only the first implementation phase. The target is not a compressed "mini Akashic clone". The target is a staged rebuild path where each milestone introduces exactly one new class of problem.

Phase 1 is intentionally narrow:

- Python monolith
- Telegram as the first user-facing channel
- Long polling, not webhook
- Single-turn text-in / text-out conversation only
- OpenAI-compatible API only
- Clear internal boundaries from day one

The purpose of this phase is to make the message-driven agent loop fully understandable before adding state, memory, tools, plugins, or proactive behavior.

## Context

The reference project, `akashic-agent`, is not a single feature. It combines several independent subsystems:

- passive conversation loop
- plugin system
- memory pipeline
- proactive push system
- drift/background task execution
- frontend and operational tooling

Trying to reproduce all of that at once would hide the reason each subsystem exists. This design instead decomposes the rebuild into milestones so each later abstraction is justified by a concrete problem encountered earlier.

## Recommended Approach

Three implementation styles were considered for Phase 1:

1. Script-first: one mostly linear file with minimal helpers
2. Thin layered monolith: a few explicit boundaries, each with current minimal responsibility
3. Full future skeleton: define many extension interfaces up front

Recommended choice: thin layered monolith.

Reasoning:

- script-first is fast, but it teaches through future pain rather than through explicit boundaries
- full future skeleton introduces abstractions before there is evidence they are needed
- thin layered monolith keeps the system small while making the main change points visible

This gives a good learning path: enough structure to understand why boundaries matter, but not so much structure that the first milestone becomes architecture theater.

## Phase 1 Scope

Phase 1 includes exactly this runtime path:

`Telegram long polling -> receive one text message -> call OpenAI-compatible LLM -> send one text reply`

What is included:

- one Telegram bot process
- polling loop
- plain text message intake
- one agent service method that turns user text into assistant text
- one LLM client for OpenAI-compatible chat completion style requests
- startup config loading and validation
- basic logs at key chain points
- minimal unit tests for local logic

What is explicitly excluded:

- multi-turn history
- short-term session state
- long-term memory
- vector retrieval
- tool calling
- plugin lifecycle
- proactive messaging
- drift/background skills
- webhook mode
- dashboard/frontend
- streaming output
- advanced Telegram formatting and media handling

## Architecture

Phase 1 should keep four explicit boundaries.

### 1. Telegram Adapter

Responsibility:

- poll Telegram for updates
- extract plain user text
- send plain text replies

Non-responsibility:

- prompt assembly
- model invocation logic
- business decisions about how to answer

Why this boundary exists:

Telegram is channel-specific noise. Later, if the channel changes to CLI, web, or another platform, the core agent logic should remain intact.

### 2. Agent Service

Responsibility:

- accept one user text input
- produce one assistant text output

Phase 1 internal behavior can stay simple:

- receive text
- create the request payload for the model
- call the LLM client
- return reply text

Why this boundary exists:

This is the future growth point for conversation history, memory injection, tool orchestration, and policy logic. Even if Phase 1 logic is small, the boundary prevents channel code from owning business behavior.

### 3. LLM Client

Responsibility:

- send a request to an OpenAI-compatible endpoint
- return the response text

Non-responsibility:

- deciding what the prompt should be
- understanding Telegram updates

Why this boundary exists:

Business logic and transport logic change for different reasons. Base URL, model name, timeout, retry, and auth handling should be adjustable without rewriting agent behavior.

### 4. Config

Responsibility:

- load configuration from environment variables or one centralized source
- validate required values at startup

Core config for Phase 1:

- `BOT_TOKEN`
- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `OPENAI_MODEL`

Why this boundary exists:

Scattered config is low-value complexity. Central validation keeps startup failure deterministic and prevents runtime confusion.

## Suggested Directory Layout

Keep the structure intentionally small:

```text
app/
  main.py
  config.py
  agent.py
  llm_client.py
  telegram_bot.py
```

This is intentionally not split into deep folders such as `domain/`, `adapters/`, `interfaces/`, or `services/`.

Reasoning:

- the first milestone is too small to justify heavier project ceremony
- the boundaries should exist, but they do not need framework-sized packaging
- future restructuring should be driven by real growth, not by anticipation

## Data Flow

Phase 1 should preserve a straight, inspectable chain:

`Telegram Update -> telegram adapter -> agent service -> llm client -> telegram adapter -> Telegram reply`

### Input handling

The Telegram adapter should only pass plain text into the agent service.

Ignored in Phase 1:

- images
- audio
- callback buttons
- complex group metadata

Reasoning:

The goal is to isolate the main message-driven path. Telegram feature surface should not obscure the core mechanics.

### Prompt handling

The agent service should create a minimal single-turn prompt.

Example shape:

- fixed system instruction
- user message as the only user content

Reasoning:

The first milestone is about understanding ownership boundaries, not prompt optimization. The agent service owns "what to ask"; the LLM client owns "how to send it".

### Output handling

The system should return one plain text reply.

Ignored in Phase 1:

- streaming
- markdown correction
- long-message chunking
- rich Telegram response types

Reasoning:

These are output quality improvements, not prerequisites for proving the architecture works.

## Why Phase 1 Excludes Multi-Turn Context

Multi-turn context is intentionally deferred to the next milestone.

Reasoning:

- it introduces a different problem class: state management
- once history exists, the system must answer where it is stored, how much is kept, when it is trimmed, and how recovery works
- if Phase 1 includes both message flow and state flow, it becomes harder to identify which architecture exists for which reason

Single-turn behavior is limited, but it isolates the first-order problem:

- channel integration
- input transformation
- model invocation
- output return

Phase 1 is complete when these three checks hold reliably:

- the bot receives a Telegram text message
- the system sends that text to the model
- the system returns model text back to Telegram

## Error Handling

Phase 1 should keep error handling narrow and boundary-local.

### Telegram failures

Examples:

- polling error
- reply send failure

Handling:

- log the failure
- continue polling where reasonable
- do not crash the entire process because one reply failed

### LLM failures

Examples:

- timeout
- authentication failure
- rate limit
- malformed response

Handling:

- the LLM client raises a clear error for the upper layer
- the agent service does not hide the root category
- the Telegram layer returns a fixed fallback message to the user, such as a temporary model-unavailable notice

### Config failures

Examples:

- missing bot token
- missing API key
- missing base URL
- missing model

Handling:

- fail fast at startup
- print the missing or invalid fields
- do not enter the runtime loop

Reasoning:

Configuration problems are not runtime resilience issues. They should be blocked before the app starts.

## Logging

Logs only need to answer these questions:

- what message was received
- whether model invocation started
- whether model invocation succeeded
- whether the Telegram reply was sent

This means logs should be placed around chain nodes, not around speculative framework concerns.

Phase 1 does not need:

- distributed tracing
- metrics pipeline
- request correlation framework
- advanced observability tooling

Reasoning:

The first milestone should observe the product chain, not build a second system to observe the first system.

## Testing

Testing should cover the parts that are locally deterministic.

Should exist:

- unit tests for `agent.py`
- config validation tests for `config.py`

Phase 1 testing approach:

- mock the LLM client when testing the agent service
- verify missing config fails clearly

Can be deferred:

- automated Telegram end-to-end tests
- automated real-model integration tests

Reasoning:

External dependency E2E automation is expensive in the first milestone and gives low learning return. Hand-driven integration is enough once the local boundaries are tested.

## Verification Standard

Phase 1 should be considered done only if both layers pass:

### Automated verification

- agent service tests pass
- config validation tests pass

### Manual verification

- send a message to the Telegram bot
- confirm the bot receives it
- confirm the bot calls the model
- confirm the bot replies with model output

## Milestone Roadmap

This roadmap is part of the design because the main objective is understanding why complexity appears.

### Milestone 1: Single-turn passive conversation

New problem introduced:

- basic message-driven agent loop

Focus:

- channel boundary
- agent boundary
- model boundary
- config boundary

### Milestone 2: Multi-turn context

New problem introduced:

- session state management

Questions this milestone should answer:

- where history lives
- how much history is sent
- when history is trimmed
- how failures affect session continuity

### Milestone 3: Readable file-based memory

New problem introduced:

- durable information beyond live chat history

Preferred first form:

- readable files such as `RECENT_CONTEXT.md` and `MEMORY.md`

Why this comes before databases:

- observability is more important than optimization at this stage
- the builder should be able to inspect memory artifacts directly

### Milestone 4: Tool use

New problem introduced:

- controlled external action and retrieval

This is where the system should justify a tool abstraction instead of hardcoding all external behavior into agent logic.

### Milestone 5: Plugins and proactive capabilities

New problem introduced:

- scaling system complexity and behavior scheduling

This is where plugin lifecycle and proactive loops become justified. They should not appear before real change pressure exists.

## Design Principle

Each milestone should introduce exactly one new systemic problem.

That rule is the core of this design. The point is not merely to build a smaller version of a larger project. The point is to make each future abstraction legible by tying it to a specific pressure:

- state creates the need for context management
- persistence creates the need for memory structure
- external actions create the need for tools
- expanding capability surface creates the need for plugins and scheduling

If multiple problem classes are introduced at once, the reason for each abstraction becomes much harder to understand.

## Decision Summary

Phase 1 decisions:

- build a personal-use agent, not a full reference-project clone
- use Python monolith architecture
- use Telegram as the first interface
- use long polling as the first transport
- support only OpenAI-compatible model APIs
- optimize for explicit boundaries, not minimum line count
- intentionally exclude history, memory, tools, plugins, and proactive behavior from the first milestone

## Out of Scope for This Spec

This document does not define:

- the implementation plan task breakdown
- exact package choices for Telegram or HTTP clients
- deployment topology
- production hardening

Those should be decided only after the Phase 1 design is accepted and implementation planning begins.
