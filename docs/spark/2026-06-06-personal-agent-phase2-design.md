# Personal Agent Phase 2 Design

## Goal

Extend the Phase 1 single-turn Telegram agent into a multi-turn agent while introducing exactly one new systemic problem: per-chat session state management.

Phase 2 remains intentionally narrow:

- Telegram is still the only channel
- long polling is still the transport
- OpenAI-compatible chat completion is still the only model interface
- conversation history exists only in process memory
- history is scoped by Telegram `chat_id`
- only the most recent `N` rounds are kept

The target is not durable memory, retrieval, or tool use. The target is to make the first stateful version of the agent understandable.

## Context

Phase 1 established four boundaries:

- Telegram adapter
- agent service
- LLM client
- config

That phase proved the basic message loop:

`Telegram -> user text -> LLM -> assistant text -> Telegram`

Phase 2 should preserve those boundaries and add only the minimum new abstraction needed to answer these questions clearly:

- where conversation history lives
- who reads it
- who updates it
- how it is trimmed
- what happens when two messages arrive for the same chat close together

## Recommended Approach

Three implementation styles were considered:

1. keep history directly inside `AgentService`
2. add a dedicated in-memory `ConversationStore`
3. let `TelegramBot` own chat history

Recommended choice: dedicated in-memory `ConversationStore`.

Reasoning:

- storing history directly in `AgentService` mixes orchestration and state ownership
- storing history in `TelegramBot` couples session logic to a specific channel
- a small store boundary keeps the new state problem visible without introducing persistence

Two concurrency styles were also considered:

1. no explicit serialization for same-chat requests
2. serialize processing per `chat_id`

Recommended choice: serialize processing per `chat_id`.

Reasoning:

- once history exists, ordering matters
- if the same chat sends two messages while the first is waiting on the LLM, concurrent updates can corrupt prompt order
- per-chat serialization keeps the behavior natural without introducing cross-chat bottlenecks

## Phase 2 Scope

Phase 2 includes exactly this runtime path:

`Telegram long polling -> receive text message -> load recent chat history -> call OpenAI-compatible LLM with history -> save successful round -> send one text reply`

What is included:

- per-chat in-memory conversation history
- recent-round trimming
- same-chat sequential processing
- small tests for state and ordering behavior

What is explicitly excluded:

- persistence across process restarts
- file-based memory
- vector retrieval
- tool calling
- plugin lifecycle
- proactive messaging
- token-based truncation
- summarization of long histories
- cross-process coordination

## Architecture

Phase 2 keeps the Phase 1 boundaries and adds one new explicit boundary.

### 1. Telegram Adapter

Responsibility:

- poll Telegram for updates
- extract `chat_id` and plain user text
- send plain text replies

Non-responsibility:

- session history storage
- prompt assembly
- trimming policy

Why this boundary exists:

Telegram remains transport-only code. Adding multi-turn behavior must not turn the channel adapter into the owner of conversation state.

### 2. Agent Service

Responsibility:

- accept `chat_id` and current user text
- enter same-chat serialized processing
- read recent history
- assemble model messages
- call the LLM client
- on success, write the completed round back to the store
- return one assistant text output

Why this boundary exists:

The agent service remains the orchestration layer. It decides how history becomes prompt messages, but it does not own storage internals.

### 3. Conversation Store

Responsibility:

- store conversation history by `chat_id`
- return recent rounds for one chat
- append a successful round
- trim history to the most recent `N` rounds
- provide per-chat serialization support

Non-responsibility:

- LLM prompt design
- Telegram API behavior
- persistence to disk or database

Why this boundary exists:

Phase 2 introduces state management. A dedicated store makes that new problem legible without prematurely introducing durable storage.

### 4. LLM Client

Responsibility:

- send a request to an OpenAI-compatible endpoint
- return assistant reply text

Non-responsibility:

- history storage
- trimming policy
- same-chat sequencing

Why this boundary exists:

Transport concerns still change independently from agent behavior and session state.

### 5. Config

Responsibility:

- keep existing Phase 1 settings
- add one setting for history size, such as `conversation.max_rounds`

Why this boundary exists:

History length is runtime policy, not hardcoded business logic.

## Suggested Directory Layout

Keep the structure small and local to current growth:

```text
app/
  main.py
  config.py
  agent.py
  llm_client.py
  telegram_bot.py
  conversation_store.py
```

This remains a thin monolith. No extra packaging layers are needed yet.

## Data Model

Phase 2 should treat a round as:

- one user message
- one assistant reply

A store entry should represent completed rounds only.

Suggested conceptual shape:

- `chat_id -> list[ConversationTurn]`

Where each `ConversationTurn` contains:

- `user_text`
- `assistant_text`

Reasoning:

- Phase 2 history is meant to support prompt reconstruction
- storing only completed rounds keeps failure semantics simple
- this avoids prematurely generalizing into arbitrary event logs

## Data Flow

One request should follow this exact sequence:

1. `TelegramBot` receives an update and extracts `chat_id` and `user_text`
2. `AgentService` starts processing for that `chat_id`
3. `AgentService` enters the serialized section for that chat
4. `ConversationStore` returns the recent `N` rounds for the chat
5. `AgentService` builds messages:
   - fixed system prompt
   - historical user and assistant messages expanded from stored rounds
   - current user message
6. `LLMClient` sends the request
7. if the LLM call succeeds, `ConversationStore` appends the new completed round and trims to the most recent `N`
8. `AgentService` returns the assistant reply
9. `TelegramBot` sends the reply back to Telegram

This preserves a straight chain while making the new state step explicit.

## History Policy

Phase 2 history policy is intentionally simple:

- history is keyed by Telegram `chat_id`
- only the most recent `N` rounds are kept
- `N` is configured centrally
- trimming is round-based, not token-based
- a round enters history only after the LLM successfully returns a reply

Reasoning:

- round-based trimming is easier to explain than token budgeting
- token-aware truncation is a separate future problem
- successful-round-only history keeps stored context semantically clean

## Concurrency and Ordering

For the same `chat_id`, requests must be processed sequentially.

Required behavior:

- if one message is already being processed for a chat, the next message for that same chat waits
- different `chat_id` values remain independent

Reasoning:

- prompt history is order-sensitive
- concurrent same-chat updates can produce inconsistent context
- per-chat serialization keeps behavior intuitive without imposing a global lock

Phase 2 does not need:

- distributed locking
- multi-process consistency
- queue infrastructure

Those are later scaling concerns, not current design goals.

## Error Handling

Error handling should stay narrow and explicit.

### LLM failures

Examples:

- timeout
- authentication failure
- malformed response

Handling:

- the request returns a fallback reply to the user, as in Phase 1
- no new history is written for that request

Reasoning:

History should represent successful completed rounds, not failed attempts.

### Telegram send failures

Handling:

- log the send failure
- the completed round may remain in history if the LLM call already succeeded

Reasoning:

The conversation state represents what the agent produced, not guaranteed delivery confirmation.

### Store failures

In the expected Phase 2 design, the in-memory store should be simple enough that failures are rare and indicate programming issues rather than normal runtime conditions.

Handling:

- fail loudly in logs
- do not silently invent recovery behavior

Reasoning:

This phase is still about understanding the core design, not hiding internal consistency bugs.

## Logging

Logs should answer these questions:

- which chat received a message
- whether history was loaded
- whether the LLM call started and succeeded
- whether a round was stored
- whether a Telegram reply was sent

Phase 2 still does not need advanced observability tooling.

## Testing

Testing should focus on deterministic local behavior.

Should exist:

- agent tests for first-turn and history-aware prompt assembly
- store tests for append and trim behavior
- tests that failed LLM calls do not write history
- tests that different chats remain isolated
- tests that same-chat processing is sequential

Can be deferred:

- real Telegram concurrency tests
- token-budget truncation tests
- restart recovery tests

## Verification Standard

Phase 2 should be considered done only if both layers pass.

### Automated verification

- existing Phase 1 tests still pass
- new history and store tests pass

### Manual verification

- send multiple messages in one Telegram chat and confirm later replies reflect recent context
- exceed the configured round limit and confirm older context no longer affects replies
- send messages from two different chats and confirm histories stay separate

## Milestone Relationship

Phase 2 introduces exactly one new systemic problem:

- session state management

It intentionally does not introduce:

- durable memory design
- retrieval design
- tool orchestration
- plugin boundaries

Those remain later milestones because they solve different pressures:

- persistence creates the memory-structure problem
- external actions create the tool-control problem
- expanding behavior surface creates the plugin and scheduling problem

## Decision Summary

Phase 2 decisions:

- store history in memory only
- scope history by Telegram `chat_id`
- keep only the most recent `N` rounds
- represent history as completed user/assistant rounds
- add a dedicated `ConversationStore`
- serialize processing per `chat_id`
- write history only after successful LLM completion
- keep Telegram and LLM boundaries otherwise unchanged

## Out of Scope for This Spec

This document does not define:

- persistence format
- database schema
- token counting strategy
- summarization strategy for long histories
- implementation task breakdown

Those should be decided only after this design is accepted and implementation planning begins.
