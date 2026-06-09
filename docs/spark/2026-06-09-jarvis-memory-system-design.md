# Jarvis Memory System Design

## Overview

This spec defines the first real memory architecture for `jarvis-agent`.

The goal is not just to let Jarvis "remember things".

The goal is to introduce a memory subsystem that is:

- replaceable
- easy to iterate on
- compatible with the current runtime direction
- close in spirit to `akashic-agent` without copying its implementation blindly

This design adopts a mixed memory model:

- long-term user profile and preferences
- task and factual memory
- recent conversation summary
- semantic retrieval through embeddings

It also adopts the same high-level shape as Akashic:

- Markdown memory layer for human-readable promptable memory
- vector memory layer for semantic recall
- lightweight post-turn consolidation
- lower-frequency background optimization

Within that shape, `SELF.md` and `MEMORY.md` are full-injection memory files.

They are not truncated and are not retrieval-gated during prompt assembly.

That is why the optimizer must keep them compact.

## Product Goal

This phase is successful when Jarvis has a memory subsystem that:

- can be swapped or refactored without rewriting the passive pipeline
- supports both prompt-visible memory and semantic retrieval
- keeps short-term recent turns separate from long-term memory
- does not make passive replies fragile when memory components fail
- leaves a clean path for future memory tools, dashboarding, and proactive behavior

## Scope

### In Scope

- context and memory subsystem boundary
- recent-turn context absorption of the current session store role
- Markdown memory files
- vector retrieval layer
- embedding provider abstraction
- consolidation and optimizer lifecycle
- prompt injection order
- integration with passive and proactive runtime paths
- consistency, idempotency, and degradation rules

### Out of Scope

- final dashboard UX
- multi-user memory isolation policy beyond current session semantics
- production-grade vector database choice
- final memory tool UX
- memory permission/approval workflow
- final prompt wording for each internal maintenance call

## Recommended Approach

Three directions were considered:

1. Akashic-aligned layered memory subsystem
2. database-first memory system with Markdown as projection
3. minimal bolt-on memory service around the current passive pipeline

The recommended direction is `1`.

Reasoning:

- it best matches the requirement that memory be replaceable and easy to iterate on
- it separates short-term context from long-term memory cleanly
- it fits the current runtime architecture direction already documented in this repository
- it supports both human-readable memory and semantic retrieval from the beginning
- it preserves a path toward tools, proactive memory work, and future UI/admin surfaces

## Core Architecture

Jarvis should introduce a dedicated `Context/Memory` subsystem.

The current `SessionStore` should not remain the long-term architectural boundary.

Instead:

- the recent-turn window capability is preserved
- the current store concept is absorbed into a broader context layer
- long-term and retrievable memory are handled by a separate memory engine

The subsystem should be organized around these components:

### `ContextStore`

Responsibilities:

- persist recent raw user/assistant/tool turns
- return a recent-turn window for prompt assembly
- trim older turns according to configured limits

This is the replacement for the useful part of the current `SessionStore`.

It is not a long-term memory system.

### `MemoryEngine`

Responsibilities:

- provide a stable high-level memory interface
- retrieve memory for passive turns
- retrieve memory for explicit future memory tools
- write, forget, or correct memory
- coordinate Markdown memory and vector memory implementations

This is the primary replaceable boundary.

Callers should depend on its protocols, not on any specific storage implementation.

### `MemoryConsolidator`

Responsibilities:

- run a consolidation check after each committed agent turn
- decide whether enough new conversation has accumulated to run full consolidation
- extract history events and pending long-term memory candidates
- refresh recent-context summary

This should be lightweight relative to long-term optimization.

### `MemoryOptimizer`

Responsibilities:

- run on a lower-frequency scheduler/proactive path
- consume pending memory items
- update long-term Markdown memory
- update self-model memory
- reconcile corrections and duplicates

This component exists specifically so that high-frequency turn handling does not keep rewriting long-term prompt memory.

The default runtime behavior is a background optimizer task enabled at agent startup.

Suggested defaults:

- `memory_optimizer_enabled = true`
- `memory_optimizer_interval_seconds = 64800`

### `PromptContextAssembler`

Responsibilities:

- build the full memory-aware prompt context for a turn
- merge recent raw turns, recent summaries, long-term memory, self-model, and semantic recall

This replaces the current very thin context assembly logic.

## Memory Layers

The default implementation should have two memory layers.

### 1. Markdown Memory Layer

This layer is:

- human-readable
- easy to inspect
- directly usable for prompt injection
- suitable for lower-frequency curated memory

### 2. Vector Memory Layer

This layer is:

- embedding-based
- semantic-retrieval oriented
- suitable for detailed or non-obvious recall
- independent from Markdown files except through explicit coordination events

The existence of the vector layer means the design requires an embedding model abstraction.

The design must not bind Jarvis to a single embedding provider.

## Markdown File Set

The default Markdown memory set should include:

- `data/memory/SELF.md`
- `data/memory/MEMORY.md`
- `data/memory/RECENT_CONTEXT.md`
- `data/memory/HISTORY.md`
- `data/memory/PENDING.md`
- `data/memory/journal/YYYY-MM-DD.md`

### `SELF.md`

Purpose:

- store Jarvis self-model information
- store Jarvis' stable understanding of its relationship with the user
- store durable behavioral framing or role understanding

Rules:

- it is maintained primarily by the optimizer
- it is separate from user long-term memory
- it should stay compact enough for full prompt injection

`SELF.md` is a full-injection file and should remain compact enough to inject in full on every turn.

### `MEMORY.md`

Purpose:

- store durable user facts, preferences, task/fact memory, and resolved long-term information

Rules:

- it is maintained primarily by the optimizer
- it should be prompt-friendly and compact
- it should not be used as a high-frequency write target

`MEMORY.md` is a full-injection file.

It is not truncated and is not retrieval-gated during prompt assembly.

### `RECENT_CONTEXT.md`

Purpose:

- store compressed recent context
- capture ongoing threads and short-term continuity at a summary level

Rules:

- its `Compression` block is updated by consolidation
- it contains compressed recent context, not the only source of recent raw turns
- assistant suggestions must not be converted into user facts during compression
- the `Compression` block is generated only from `USER` messages
- the `Recent Turns` block is refreshed separately after each turn without requiring full consolidation

Suggested structure:

- `# Recent Context`
- `## Compression`
- `## Ongoing Threads`
- `## Recent Turns`

The `Recent Turns` block is a lightweight view and does not replace the separate recent-turn context capability.

The `Compression` block is produced by the second consolidation LLM call and should follow strict extraction rules rather than freeform assistant-authored summarization.

### `HISTORY.md`

Purpose:

- store a human-readable event log of consolidated conversation history
- act as a source for grep-style lookup and traceability
- help connect Markdown memory to vector ingestion

Rules:

- append-only
- not directly injected into the main prompt
- used for grep-style lookup and as traceable context for consolidation itself
- each entry includes an invisible consolidation marker for idempotency

Each entry should be represented as:

```html
<!-- consolidation:["msg_id1","msg_id2"]:history_entry -->
```

followed by one readable line such as:

```text
[2026-05-09 14:30] User started learning Rust and bought the second edition of The Rust Programming Language.
[2026-05-09 15:00] User said they do not like oppressive mystery-style games.
```

The hidden marker exists for deduplication and traceability.

The visible line exists for human readability.

### `PENDING.md`

Purpose:

- act as a staging buffer for extracted long-term memory candidates
- separate high-frequency memory extraction from lower-frequency long-term memory rewriting

Rules:

- written by consolidation
- consumed by optimizer
- not injected directly into the main prompt
- supports snapshot/commit/rollback semantics during optimization

The initial tag set should align with Akashic's six tags:

- `identity`
- `preference`
- `key_info`
- `health_long_term`
- `requested_memory`
- `correction`

The protocol should allow future extension beyond these six tags.

### `journal/YYYY-MM-DD.md`

Purpose:

- store per-day journalized copies of history entries
- support chronological browsing without relying on one oversized history file

Rules:

- append-only
- mirrors consolidated history entries for the relevant day

## Short-Term Context Model

Jarvis should preserve a recent-turn raw window even after the old `SessionStore` boundary is retired.

This is necessary because:

- summaries are not enough for fine-grained follow-up turns
- raw phrasing, references, and local turn structure still matter
- coding and tool-usage workflows depend on recent exact wording

The short-term model should therefore have two distinct forms:

- `RecentTurns` as raw context window
- `RecentContext` as compressed summary

These forms serve different purposes and should not be collapsed into one object.

## Prompt Injection Order

The prompt assembly order should be:

1. base system prompt
2. `SELF.md`
3. `MEMORY.md`
4. `RECENT_CONTEXT.md`
5. vector recall block
6. recent raw turns

This keeps:

- self-model and stable identity early
- long-term memory ahead of recency summary
- semantic recall available but bounded
- exact recent phrasing available at the end for local continuity

`SELF.md` and `MEMORY.md` are full-injection candidates and therefore must remain compact.

`HISTORY.md` is not directly injected.

## Consolidation Lifecycle

Consolidation check runs after each agent reply is committed through a `TurnCommitted`-style event.

That check does not imply that full consolidation runs after every turn.

Instead, full consolidation is gated by a minimum new-message threshold measured from the last successful consolidation position.

A good default is:

- `min_new_messages = max(5, keep_count // 2)`

This preserves the intended behavior:

- do not pay full consolidation cost on every tiny exchange
- refresh the lightweight `Recent Turns` view after every turn
- run structured extraction once enough new material exists

### Consolidation Inputs

The consolidator should receive:

- recent unprocessed message batch
- message ids
- roles
- timestamps
- text content
- enough contextual grouping to define a stable `source_ref`

### Consolidation Outputs

The default logical outputs are:

- `history_entries[]`
- `pending_items[]`
- `recent_context compression`

### Consolidation LLM Call Shape

The default consolidation shape should be two LLM calls plus a separate lightweight refresh path:

- first LLM call: extract `history_entries[]` and `pending_items[]`
- second LLM call: generate the `RECENT_CONTEXT.md` `Compression` block
- separate lightweight path: refresh the `RECENT_CONTEXT.md` `Recent Turns` block after each turn

This default should be treated as the canonical first implementation shape.

The second call should follow strict rules:

- extract only from `USER` messages
- do not convert assistant suggestions into user facts
- compress recent continuity without turning the block into long-term memory

### Consolidation Writes

Consolidation should write:

- `HISTORY.md`
- `PENDING.md`
- `RECENT_CONTEXT.md`
- `journal/YYYY-MM-DD.md`

When the threshold is not met, the system should still:

- update the recent-turn window
- refresh the `Recent Turns` portion of `RECENT_CONTEXT.md`

without running the two-call full consolidation path.

### Consolidation Idempotency

Consolidation must be idempotent.

The same message batch must not produce duplicate history or pending entries after retries.

The preferred identity basis is:

- a `source_ref`
- or stable grouped message ids

The invisible `HISTORY.md` markers and corresponding database metadata should both reinforce this guarantee.

## Optimizer Lifecycle

The optimizer should run out of the passive reply hot path.

Its natural execution home is the scheduler/proactive side of the runtime.

The optimizer is responsible for:

- consuming `PENDING.md`
- updating `MEMORY.md`
- updating `SELF.md`
- merging or replacing conflicting facts
- applying corrections
- ignoring duplicates
- keeping prompt-injected memory compact

This separation exists to reduce churn in long-term prompt memory and to keep passive turns lighter.

The default optimizer flow should be:

1. read `MEMORY.md` and `PENDING.md`
2. run an LLM archival pass that decides, for each pending item, whether it is:
   - a new fact to add
   - a conflicting fact that should update or replace an existing entry
   - a duplicate that should be ignored
   - a correction that should overwrite the corrected old entry
3. write the updated `MEMORY.md`
4. update `SELF.md` when relevant self-model changes are part of the optimization pass
5. `clear_pending()` only after the write succeeds

This is why `PENDING.md` must support recoverable processing semantics rather than acting as a fire-and-forget queue.

## Vector Memory Design

The vector layer should be driven by abstractions, not by vendor-specific logic.

### `EmbeddingProvider`

Requirements:

- mandatory protocol for vector memory
- no hard dependency on one model vendor
- replaceable default implementation

Typical responsibility:

- transform text chunks into embeddings

### `VectorStore`

Requirements:

- store embedding-backed memory records
- support semantic search
- support deletion or state updates when needed
- support metadata filters such as time, tag, or source

### Vector Ingestion Timing

Vector ingestion should happen after successful consolidation commit.

That means:

- Markdown memory writes can complete first
- a `ConsolidationCommitted`-style event can trigger vector ingestion
- vector indexing may be eventually consistent

This should not block passive reply completion.

## Interfaces and Protocols

Jarvis should define explicit interfaces before finalizing concrete implementations.

### `ContextStore`

Expected capabilities:

- `append_turn(...)`
- `get_recent_turns(...)`
- `trim_session(...)`

### `MemoryRetrievalApi`

Expected capabilities:

- `retrieve_for_turn(...)`
- `retrieve_explicit(...)`
- `retrieve_for_proactive(...)`

### `MemoryWriteApi`

Expected capabilities:

- `remember(...)`
- `forget(...)`
- `correct(...)`

### `MemoryMaintenanceApi`

Expected capabilities:

- `on_turn_committed(...)`
- `should_consolidate(...)`
- `consolidate(...)`
- `optimize(...)`

### `EmbeddingProvider`

Expected capability:

- `embed_texts(...)`

### `VectorStore`

Expected capabilities:

- `upsert_items(...)`
- `search(...)`
- `delete_items(...)`

The default `MemoryEngine` should compose these dependencies rather than exposing storage details directly to pipelines.

Even before full vector retrieval is implemented, these interfaces should exist so the boundary is fixed early and concrete implementations can evolve behind it.

## File and Data Structure Guidance

The memory subsystem should live in its own package rather than being hidden under the old narrow state folder.

Suggested package shape:

- `jarvis/memory/engine.py`
- `jarvis/memory/context.py`
- `jarvis/memory/consolidation.py`
- `jarvis/memory/optimizer.py`
- `jarvis/memory/embeddings.py`
- `jarvis/memory/vector_store.py`
- `jarvis/memory/markdown_store.py`
- `jarvis/memory/models.py`

The default implementation may also use a local metadata/vector database such as:

- `data/memory/memory.db`

That local database may store:

- vector records
- source-ref idempotency records
- optimizer snapshot state
- recent-turn window records
- maintenance progress metadata
- last successful consolidation position

## Integration With Current Jarvis Runtime

This design should be integrated incrementally.

It does not require a top-level runtime rewrite.

### Passive Pipeline

The current passive path should evolve as follows:

- `PassiveContextAssembler` becomes memory-aware prompt assembly
- `PassiveTurnFinalizer` stops being just a reply append point and becomes a turn-commit producer
- `PassivePipeline` uses:
  - `PromptContextAssembler`
  - `MemoryMaintenanceApi`

After a turn completes:

1. recent turns are committed to `ContextStore`
2. a normalized turn-committed payload is created
3. the memory subsystem decides whether to consolidate

### Runtime Bootstrap

`runtime/bootstrap.py` should assemble:

- `ContextStore`
- `MemoryEngine`
- `EmbeddingProvider`
- `VectorStore`

and expose them through the runtime container.

### Proactive/Scheduler Side

The optimizer should attach to the scheduler/proactive side because:

- it is background maintenance work
- it should not lengthen passive reply latency
- the current runtime already has a scheduler tick seam

The current empty `ProactivePipeline` is a natural future host for this behavior.

The intended first implementation is a background task registered at main agent startup and controlled by the optimizer enable/interval settings.

## Error Handling and Degradation

Memory must not be allowed to break the core reply path.

### Degradation Rules

- if `ContextStore` fails, the system should still be able to reply using at least the current user message and base system prompt
- if long-term retrieval fails, the system should still reply using recent-turn context when available
- if embeddings or vector retrieval fail, Markdown memory should still function
- if consolidation fails, the current reply should still succeed and the failure should be logged
- if optimizer fails, pending memory must remain recoverable for a later run

### Consistency Rules

1. recent-turn commit is the most important synchronous context write
2. consolidation must be idempotent
3. pending-to-memory optimization must use snapshot or transaction-like semantics
4. vector consistency may be eventual rather than immediate

## Testing Strategy

The implementation should be introduced in phases and tested accordingly.

### Phase 1: Boundary Introduction

Goals:

- introduce `ContextStore`
- introduce memory interfaces
- route passive context assembly through the new boundary
- define `EmbeddingProvider` and `VectorStore` interfaces even if their initial implementations are placeholders

Acceptance:

- current passive replies still work
- memory can be disabled without breaking the system

### Phase 2: Markdown Maintenance

Goals:

- implement `SELF.md`, `MEMORY.md`, `RECENT_CONTEXT.md`, `HISTORY.md`, `PENDING.md`
- implement consolidation
- implement optimizer

Acceptance:

- consolidation idempotency works
- pending snapshot safety works
- recent context appears in assembled prompt

### Phase 3: Vector Retrieval

Goals:

- implement embedding abstraction
- implement vector store integration
- inject bounded semantic recall into the prompt

Acceptance:

- vector retrieval can be swapped independently of Markdown storage
- vector layer failures do not break passive replies

### Test Areas

- recent-turn trimming
- prompt injection ordering
- consolidation threshold logic
- consolidation idempotency
- hidden marker handling in `HISTORY.md`
- pending snapshot commit/rollback
- optimizer rewrite logic for `MEMORY.md` and `SELF.md`
- degradation when vector/embedding components fail
- scheduler/proactive-triggered optimizer paths

## Non-Goals For This Spec

This spec does not define:

- the final embedding model provider
- the final vector store product choice
- the final memory tools UX
- dashboard editing flows
- exact maintenance prompts
- final multi-user memory permissioning

Those decisions should remain open as long as the architecture above is preserved.

This spec also does not prioritize making every memory behavior highly configurable from day one.

The first priority is to make the default behavior correct and stable.

Configuration surface can expand later where it clearly improves iteration without weakening the boundary design.

## Final Design Summary

Jarvis should adopt a dedicated `Context/Memory` subsystem in which:

- the old `SessionStore` boundary is retired
- recent raw turns survive inside a new `ContextStore`
- `SELF.md`, `MEMORY.md`, `RECENT_CONTEXT.md`, `HISTORY.md`, `PENDING.md`, and daily journals form the default Markdown memory layer
- vector memory is powered by an abstract embedding provider and vector store
- consolidation happens after committed turns
- long-term memory and self-model updates happen in a lower-frequency optimizer
- prompt injection follows a fixed memory-aware order
- failures degrade safely rather than breaking passive replies

This gives Jarvis a memory architecture that is usable now, compatible with the current runtime direction, and deliberately easy to replace or iterate on later.
