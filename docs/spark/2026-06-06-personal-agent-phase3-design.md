# Personal Agent Phase 3 Design

## Goal

Extend the Phase 2 multi-turn Telegram agent into a personal agent with durable, human-readable long-term memory, while introducing exactly one new systemic problem:

- how long-term memory persists beyond process-local conversation history

Phase 3 remains intentionally narrow:

- Telegram is still the only channel
- long polling is still the transport
- OpenAI-compatible chat completion is still the only model interface
- short-term conversation history remains in the in-memory `ConversationStore`
- long-term memory is global personal memory, not per-chat memory
- long-term memory is stored as readable local Markdown files

The target is not automated memory extraction, consolidation, retrieval policy, or tool use. The target is to establish a durable memory boundary that survives restarts and is inspectable by a human.

## Context

Phase 2 introduced exactly one new problem:

- per-chat session state management

That phase added an in-memory `ConversationStore`, round-based trimming, and same-chat serialized processing. The current runtime can preserve recent context only while the process stays alive.

The next distinct pressure is different:

- some information should outlive one process
- some information is personal-global rather than chat-local
- some information should remain directly editable and inspectable without hidden storage machinery

This phase should solve that persistence problem without also taking on the next problem:

- when long-term memory should be injected, updated, summarized, or ignored

Those are policy questions and belong to Phase 4.

## Relationship to Akashic-Agent

The reference project, `akashic-agent`, uses a richer Markdown memory model with multiple files and automated flows around them. That reference is useful for understanding file responsibilities, but its full design already includes policy and consolidation concerns that would exceed this phase.

Phase 3 intentionally borrows only the readable file-surface ideas that fit the current boundary:

- `MEMORY.md`
- `RECENT_CONTEXT.md`
- `PENDING.md`
- `HISTORY.md`

It intentionally does not adopt, in this phase:

- automated consolidation workers
- optimizer-style memory rewriting
- vector-backed retrieval
- separate self-model files such as `SELF.md`

## Recommended Approach

Three design styles were considered:

1. store all long-term memory in one combined Markdown file
2. use multiple formal memory files behind a dedicated `MemoryStore`
3. introduce formal memory files plus an extra raw candidate-event inbox

Recommended choice: multiple formal memory files behind a dedicated `MemoryStore`, without a separate inbox file.

Reasoning:

- a single combined file is initially simple, but it blurs stable facts, recent background, pending items, and important events
- a raw inbox adds a fourth write surface whose role overlaps with the formal memory files
- a dedicated `MemoryStore` with a small number of clearly differentiated files keeps the persistence problem visible without prematurely introducing memory policy

This creates a narrow and explainable boundary:

- the filesystem is the current storage substrate
- file roles are explicit
- consumers depend on a read model, not on ad hoc path access

## Phase 3 Scope

Phase 3 includes exactly this change in product shape:

`Telegram long polling -> receive text message -> load short-term chat history -> load long-term memory snapshot -> call OpenAI-compatible LLM -> send one text reply`

What is included:

- a dedicated `MemoryStore`
- durable memory files under a local `memory/` workspace directory
- global personal memory, shared across chats
- a unified long-term memory read model
- direct human inspection and manual editing of memory files
- stable startup initialization for missing memory files
- tests for deterministic memory storage and read behavior

What is explicitly excluded:

- automatic memory extraction from messages
- automatic promotion of facts into long-term memory
- automatic rewriting of memory files after each turn
- retrieval ranking or semantic search
- token-aware memory selection
- vector retrieval
- tool calling
- plugin lifecycle
- proactive messaging
- background consolidation jobs

## Architecture

Phase 3 keeps all Phase 2 boundaries and adds one new explicit boundary.

### 1. Telegram Adapter

Responsibility:

- poll Telegram for updates
- extract `chat_id` and plain user text
- send plain text replies

Non-responsibility:

- long-term memory storage
- long-term memory file access
- memory prompt assembly decisions

Why this boundary exists:

Telegram remains transport-only code. Adding durable memory must not make the channel adapter responsible for persistence.

### 2. Agent Service

Responsibility:

- accept `chat_id` and current user text
- enter same-chat serialized processing
- read recent short-term history from `ConversationStore`
- read long-term memory through `MemoryStore`
- assemble model messages from fixed prompt parts
- call the LLM client
- on success, store the completed round in `ConversationStore`
- return one assistant text output

Non-responsibility:

- owning filesystem paths
- manually opening memory files
- deciding which newly-seen facts should be promoted into long-term memory

Why this boundary exists:

The agent service remains the orchestration layer. It may consume long-term memory, but it should not absorb storage mechanics or future memory policy.

### 3. Conversation Store

Responsibility:

- keep per-chat in-memory short-term conversation history
- trim recent rounds
- provide same-chat serialization support

Non-responsibility:

- durable storage
- long-term personal memory

Why this boundary exists:

Phase 2 introduced short-term session state. That problem remains separate from durable personal memory.

### 4. Memory Store

Responsibility:

- own the `memory/` workspace surface
- ensure the memory directory and required files exist
- read long-term memory files
- expose a unified `MemorySnapshot` read model
- provide controlled file write operations for initialization and maintenance

Non-responsibility:

- deciding what should be remembered
- deciding what should be injected for a given turn
- automatic summarization or consolidation
- prompt budget optimization

Why this boundary exists:

Phase 3 introduces persistent memory as a new problem. A dedicated store makes the new persistence boundary explicit without mixing it into the agent service or Telegram adapter.

### 5. LLM Client

Responsibility:

- send a request to an OpenAI-compatible endpoint
- return assistant reply text

Non-responsibility:

- memory storage
- file access
- prompt policy decisions

Why this boundary exists:

Transport concerns still change independently from persistence and orchestration concerns.

### 6. Config

Responsibility:

- keep existing Phase 2 settings
- add the configurable memory root path if needed

Suggested additions:

- `memory.root_dir`

Why this boundary exists:

Memory workspace location is runtime configuration, not hardcoded business logic.

## Suggested Directory Layout

Keep the structure intentionally small:

```text
app/
  main.py
  config.py
  agent.py
  llm_client.py
  telegram_bot.py
  conversation_store.py
  memory_store.py

memory/
  MEMORY.md
  RECENT_CONTEXT.md
  PENDING.md
  HISTORY.md
```

This remains a thin monolith. No extra service layering or plugin scaffolding is needed yet.

## Memory File Model

Phase 3 should define four formal memory files.

### 1. `MEMORY.md`

Purpose:

- long-lived stable facts that should remain useful across time

Examples:

- enduring user preferences
- stable project background
- long-term constraints
- important personal facts that are unlikely to change frequently

What does not belong here:

- chat transcript fragments
- temporary work-in-progress notes
- open tasks
- routine daily updates

Reasoning:

This file is the stable fact layer, not a journal.

### 2. `RECENT_CONTEXT.md`

Purpose:

- near-term background summary that helps current replies but may age out later

Examples:

- what the user has been working on recently
- current project focus
- recently made but still active decisions
- short-term context that remains useful across several conversations

What does not belong here:

- immutable personal facts
- unresolved task inventory
- raw event logs

Reasoning:

This file is a summary view of recent context, not a transcript or a fact registry.

### 3. `PENDING.md`

Purpose:

- open loops, unresolved commitments, pending follow-ups, and items that still need closure

Examples:

- items the user asked to revisit later
- decisions waiting for confirmation
- tasks or reminders that are not yet done

What does not belong here:

- completed work
- stable long-term facts
- general background summaries

Reasoning:

This file tracks unresolved state. It exists because "what is still open" is a different concern from "what is true" or "what has happened recently."

### 4. `HISTORY.md`

Purpose:

- a limited timeline of important events that are worth preserving in time order

Examples:

- important project state transitions
- key accepted or rejected decisions
- meaningful external fact changes
- explicit completion or cancellation of long-running pending items

What does not belong here:

- ordinary chat turns
- low-signal daily progress chatter
- repeated copies of `MEMORY.md` or `RECENT_CONTEXT.md`
- exhaustive logs of everything the user said

Reasoning:

The other files are state views. `HISTORY.md` adds a controlled event view. It should stay selective so it does not collapse into a noisy transcript.

## Read Model

`MemoryStore` should expose a unified read model so the rest of the system does not reason about file paths directly.

Suggested conceptual shape:

- `MemorySnapshot.memory_text`
- `MemorySnapshot.recent_context_text`
- `MemorySnapshot.pending_text`
- `MemorySnapshot.history_text`

This can later evolve into richer structured parsing if needed, but Phase 3 should treat file contents as plain text blocks.

Reasoning:

- the phase is about persistence, not Markdown parsing
- plain-text consumption keeps the behavior legible
- later phases can refine read/write semantics without forcing a new storage boundary

## Data Flow

One request should follow this sequence:

1. `TelegramBot` receives an update and extracts `chat_id` and `user_text`
2. `AgentService` enters the same-chat serialized section
3. `ConversationStore` returns recent short-term rounds for that chat
4. `MemoryStore` returns a `MemorySnapshot`
5. `AgentService` builds messages from:
   - fixed system prompt
   - long-term memory text blocks in a fixed order
   - short-term conversation history
   - current user message
6. `LLMClient` sends the request
7. if the LLM call succeeds, `ConversationStore` appends the completed short-term round
8. `AgentService` returns the assistant reply
9. `TelegramBot` sends the reply back to Telegram

This phase intentionally stops there. It does not add a second automatic path that updates long-term memory after the reply.

## Prompt Participation Rule

Phase 3 may allow long-term memory to participate in prompt construction, but only through a simple fixed rule.

Acceptable Phase 3 behavior:

- always include the same memory sections in the same order
- include them as plain text in a stable system-context block

Not acceptable in Phase 3:

- dynamic ranking of memory sections by relevance
- selective retrieval based on semantic matching
- deciding that some messages should rewrite memory files

Reasoning:

Using durable memory in prompts is compatible with the persistence goal. Sophisticated decisions about when and how to use memory are a separate policy problem.

## Write Policy Boundary

Phase 3 should be explicit about what it does not do.

The system may support controlled write methods inside `MemoryStore`, but this phase does not require the agent to automatically modify the formal memory files during normal reply generation.

Recommended default:

- human edits maintain `MEMORY.md`, `RECENT_CONTEXT.md`, `PENDING.md`, and `HISTORY.md`
- `MemoryStore` supports initialization and safe file access
- any future automatic memory update behavior is deferred to Phase 4

Reasoning:

If Phase 3 lets the agent freely decide what to add, remove, merge, or rewrite in these files, then it is no longer solving only the storage problem. It has started solving memory policy.

## Failure Semantics

Error handling should stay explicit and narrow.

### Memory workspace initialization failure

Examples:

- memory root directory cannot be created
- required files cannot be written during startup

Handling:

- fail fast at startup
- report the path and failure category clearly
- do not continue into the polling loop

Reasoning:

This is a runtime environment problem, not a soft user-turn problem.

### Missing memory files

Handling:

- `MemoryStore.ensure_initialized()` creates missing files with minimal placeholder templates

Reasoning:

Missing files are a normal bootstrap case, not a reason for manual setup burden.

### Memory file read failure during a turn

Examples:

- transient filesystem read error
- one file deleted after startup

Handling:

- log the failure clearly
- allow the turn to degrade to short-term history only
- do not lose the reply if the LLM call can still proceed without long-term memory

Reasoning:

The system should keep replying when possible, while making the missing long-term memory participation visible in logs.

### Memory content shape problems

Handling:

- do not introduce a strict Markdown parser in Phase 3
- treat readable file contents as plain text
- reserve structural parsing for later phases if needed

Reasoning:

This phase is about durable human-readable storage, not schema-heavy content validation.

### LLM failure

Handling:

- same as Phase 2
- no new short-term round is written
- no long-term memory rewrite is attempted

Reasoning:

Long-term memory is not part of the success path for writes in this phase.

## Logging

Logs should answer these questions:

- was the memory workspace initialized
- was long-term memory loaded successfully for the turn
- did the turn use long-term memory or degrade to short-term-only mode
- did the short-term round store succeed
- was the Telegram reply sent

Phase 3 still does not need:

- distributed tracing
- memory retrieval analytics
- consolidation metrics

## Testing

Testing should focus on deterministic local behavior.

Should exist:

- `MemoryStore` initialization tests
- tests that missing files are created automatically
- tests that all four files are loaded into `MemorySnapshot`
- tests that `AgentService` includes the long-term memory blocks in fixed prompt order
- tests that memory read failure degrades gracefully to short-term-only operation

Can be deferred:

- automatic memory rewrite tests
- semantic memory selection tests
- Markdown structural parsing tests
- restart recovery beyond file existence and read behavior

## Verification Standard

Phase 3 should be considered done only if both layers pass.

### Automated verification

- existing Phase 1 and Phase 2 tests still pass
- new `MemoryStore` tests pass
- new prompt-assembly tests for long-term memory inclusion pass

### Manual verification

- start with no `memory/` directory and confirm the app initializes it
- edit the memory files manually and confirm later replies reflect the stored long-term context
- restart the process and confirm the long-term memory remains available
- remove one file and confirm it is recreated or the failure mode is explicit and understandable

## Milestone Relationship

Phase 3 introduces exactly one new systemic problem:

- durable long-term memory persistence

It intentionally does not introduce:

- memory injection policy
- memory update policy
- retrieval ranking
- summarization pipelines
- tool orchestration

Those remain later milestones because they solve different pressures:

- persistence creates the storage-boundary problem
- memory policy creates the "when and how to use memory" problem
- external actions create the tool-control problem

## Decision Summary

Phase 3 decisions:

- keep short-term chat history in `ConversationStore`
- add a dedicated `MemoryStore`
- use global personal memory rather than per-chat durable memory
- store long-term memory as readable local Markdown files
- use four formal files: `MEMORY.md`, `RECENT_CONTEXT.md`, `PENDING.md`, `HISTORY.md`
- expose a unified `MemorySnapshot` read model
- allow simple fixed-order prompt inclusion of long-term memory
- defer automatic memory updates and consolidation to a later phase

## Out of Scope for This Spec

This document does not define:

- exact Markdown templates for each file
- automatic memory extraction criteria
- automatic history event selection criteria
- semantic retrieval logic
- vector schema
- implementation task breakdown

Those should be decided only after this design is accepted and implementation planning begins.
