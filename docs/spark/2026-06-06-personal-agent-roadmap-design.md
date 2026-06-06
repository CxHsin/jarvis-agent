# Personal Agent Roadmap Design

## Goal

Define a learning-oriented roadmap from the current Phase 2 implementation toward a personal agent that approaches the usable shape of `akashic-agent`, while preserving one core rule:

- each phase introduces exactly one new systemic problem

This roadmap is intentionally not an implementation plan. It is a sequencing document that explains what should be built next, why that stage exists, which boundaries are added, which boundaries stay stable, and which boundaries should remain replaceable later if the runtime eventually outgrows a single-process design.

## Current State

The local project is no longer at a Phase 2 idea stage. It is effectively at a Phase 2 implementation state.

What already exists:

- Telegram long polling entrypoint
- single-process agent loop
- OpenAI-compatible chat client
- per-chat in-memory conversation history
- round-based history trimming
- same-chat serialized processing
- local automated tests covering the current deterministic logic

This means the roadmap should start from "what comes after in-memory multi-turn chat", not from "how to design multi-turn chat".

## Roadmap Strategy

The roadmap should optimize for understanding, not shortest path to feature parity.

That means:

- keep single-process and single-machine implementation as the default
- do not introduce distributed infrastructure early
- allow future replacement only at interface and responsibility boundaries
- add abstractions only when a newly introduced problem justifies them

The roadmap should therefore progress by problem class, not by copying capability lists from the reference project.

## Milestone Roadmap

### Phase 3: File-Based Long-Term Memory

New problem introduced:

- how information should persist beyond live chat history

Why now:

- Phase 2 introduced session state only inside process memory
- the next distinct pressure is durable information that survives process restarts

New boundary:

- `MemoryStore` or equivalent boundary for readable persistent memory artifacts

Responsibilities:

- read and write durable memory artifacts
- keep memory human-inspectable
- preserve a small, explicit memory surface

Preferred first artifacts:

- `MEMORY.md`
- `RECENT_CONTEXT.md`
- `PENDING.md`

Unchanged boundaries:

- `TelegramBot` remains transport-only
- `AgentService` remains the orchestration layer
- `LLMClient` remains transport logic for model calls
- `ConversationStore` remains short-term per-chat state only

Future replaceable boundary:

- long-term memory storage may later move away from local files, but consumers should not depend on filesystem details

Explicitly out of scope:

- vector retrieval
- embedding pipeline
- consolidation jobs
- tool calling

### Phase 4: Memory Read/Write Policy

New problem introduced:

- when long-term memory should be injected, updated, or ignored

Why now:

- once durable memory exists, the next problem is not new storage
- the next problem is deciding how memory participates in a turn

New boundary:

- `MemoryPolicy` or `MemoryManager`

Responsibilities:

- decide what durable memory becomes prompt context
- decide what information should be staged for later memory updates
- keep memory usage rules separate from storage implementation

Unchanged boundaries:

- memory persistence format
- Telegram transport
- LLM transport
- short-term conversation history

Future replaceable boundary:

- memory policy execution may later be moved or split, but storage and policy should stay separate now

Explicitly out of scope:

- semantic retrieval
- memory summarization pipeline
- optimizer/background compaction

### Phase 5: Tool Use

New problem introduced:

- how the agent performs controlled external actions or retrieval

Why now:

- long-term memory solves persistence, not action
- tool use is the first time the agent needs a controlled interface to the outside world

New boundary:

- `ToolRegistry` and `ToolExecutor`, or equivalent

Responsibilities:

- register available tools
- validate tool requests
- execute tool calls
- return structured results or failures

Unchanged boundaries:

- long-term memory boundaries
- short-term conversation store
- Telegram adapter

Future replaceable boundary:

- tool execution may later be isolated from the core process, but callers should depend only on tool invocation contracts

Explicitly out of scope:

- plugin lifecycle
- proactive scheduling
- background autonomous tasks

### Phase 6: Plugin System

New problem introduced:

- how to scale behavior extension without collapsing agent logic into hardcoded branches

Why now:

- tool use creates the first real extension surface
- once multiple behaviors want to hook into the turn lifecycle, explicit extension points become justified

New boundary:

- `PluginHost` or `PluginManager`

Responsibilities:

- load and register plugins
- expose fixed lifecycle hooks
- allow plugins to add prompt protocol, tools, or policy contributions

Unchanged boundaries:

- core turn orchestration
- tool invocation contract
- memory storage and policy boundaries

Future replaceable boundary:

- plugin execution and discovery may later evolve, but the agent core should not assume all behavior is hardcoded locally in one file

Explicitly out of scope:

- remote plugin execution
- process isolation for plugins
- proactive push behavior

### Phase 7: Proactive Messaging

New problem introduced:

- when the agent should initiate contact instead of waiting for user input

Why now:

- passive response, memory, tools, and plugin hooks should exist before adding initiative
- proactive behavior is a scheduling and decision problem, not just another message source

New boundary:

- `ProactiveLoop` or `ProactiveScheduler`

Responsibilities:

- determine poll cadence
- fetch candidate inputs for proactive decisions
- decide whether to send a message or skip

Unchanged boundaries:

- passive turn processing
- plugin system
- tool system

Future replaceable boundary:

- proactive scheduling may later move into a separate worker, but the decision interface should not assume it always shares the Telegram polling loop

Explicitly out of scope:

- distributed queues
- cross-process scheduling infrastructure
- background drift task execution

### Phase 8: Drift / Idle Background Work

New problem introduced:

- how the agent should use idle time for low-priority self-directed work

Why now:

- proactive messaging handles "should I contact the user now"
- drift handles "what should I work on when I am not replying or pushing"

New boundary:

- `DriftRunner` or `BackgroundTaskRunner`

Responsibilities:

- detect idle windows
- choose background tasks
- run tasks safely without taking ownership of passive or proactive loops

Unchanged boundaries:

- passive conversation loop
- proactive decision loop
- plugin and tool contracts

Future replaceable boundary:

- background task execution may later move out of process, but task contracts should not require in-process assumptions

Explicitly out of scope:

- full task orchestration platform
- distributed worker pools

### Phase 9: Dashboard and Inspection Interfaces

New problem introduced:

- how humans observe, inspect, and intervene once the system has multiple subsystems

Why now:

- observability and inspection become meaningful only after memory, tools, plugins, proactive behavior, and background work exist

New boundary:

- `RuntimeInspector` or equivalent read-side status interface

Responsibilities:

- expose runtime state consistently
- support CLI or dashboard inspection without bypassing core boundaries
- make internal state explainable to the operator

Unchanged boundaries:

- agent behavior logic
- memory and tool ownership
- scheduling ownership

Future replaceable boundary:

- inspection may later aggregate across processes, so dashboards and CLIs should depend on a stable read interface rather than direct object access

Explicitly out of scope:

- distributed control plane
- large-scale admin surface

### Phase 10: Runtime Stability and Deployment Constraints

New problem introduced:

- how to make the accumulated system reliable without redefining the core architecture

Why now:

- once the main product shape exists, stability and operational constraints become the next distinct pressure

Primary concerns:

- workspace layout
- config layering
- startup modes
- logging discipline
- recovery expectations
- local operator workflow

New boundary:

- this phase may add runtime coordination boundaries rather than a single new product subsystem

Unchanged boundaries:

- passive loop
- memory
- tools
- plugins
- proactive scheduler
- drift runner

Future replaceable boundary:

- runtime state, config access, and workspace access should remain explicit so later deployment evolution does not require rewriting business logic

Explicitly out of scope:

- microservice migration as a goal in itself
- premature distributed redesign

## Future Replaceable Boundaries

These boundaries should be explicitly protected in the roadmap. The goal is not to make them distributed now. The goal is to ensure that later scaling pressure changes these boundaries internally rather than forcing a rewrite of the whole system.

### 1. Conversation State Boundary

Current implementation:

- process-local in-memory store

Protected responsibility:

- per-chat history access
- same-chat ordering guarantee

What may change later:

- backing store
- ordering mechanism

What consumers must not assume:

- in-process lock details
- direct ownership of mutable history internals

### 2. Long-Term Memory Boundary

Current implementation:

- local readable files

Protected responsibility:

- durable memory persistence
- human-inspectable memory representation

What may change later:

- storage substrate
- indexing method

What consumers must not assume:

- direct filesystem layout details
- ad hoc path construction outside the boundary

### 3. Memory Policy Boundary

Current implementation:

- in-process turn-time memory decision logic

Protected responsibility:

- memory injection rules
- memory write-back rules

What may change later:

- execution placement
- policy sophistication

What consumers must not assume:

- that storage and policy are the same thing
- that every memory write must happen synchronously during reply generation

### 4. Tool Execution Boundary

Current implementation:

- in-process direct tool execution

Protected responsibility:

- tool lookup
- validation
- execution contract
- structured result handling

What may change later:

- execution isolation
- execution location

What consumers must not assume:

- that tools always run in the same process as the agent core

### 5. Scheduling Boundary

Current implementation:

- single-process local scheduling for proactive and idle work

Protected responsibility:

- deciding when non-passive work should run
- preventing scheduling concerns from leaking into channel adapters

What may change later:

- scheduler topology
- worker placement

What consumers must not assume:

- that Telegram polling is the only place where scheduling can live

### 6. Runtime Inspection Boundary

Current implementation:

- local state inspection through process-local data and workspace artifacts

Protected responsibility:

- consistent status readout for operators and tooling

What may change later:

- state aggregation method
- process boundaries for inspection

What consumers must not assume:

- direct object access to internal runtime state

## Cross-Phase Rules

### 1. One New Systemic Problem Per Phase

If a proposal introduces multiple first-order problems at once, it should be decomposed into separate phases.

### 2. Single-Process, Single-Machine By Default

The implementation path stays local-first and personal-use-first unless real pressure proves otherwise.

### 3. Replaceable Boundaries, Not Speculative Infrastructure

The design may preserve future replacement points, but should not introduce distributed systems, queueing systems, or service splits before they are needed.

### 4. Channels Remain Adapters

Telegram, CLI, dashboard, webhook, or future interfaces should remain entry and exit adapters rather than owners of memory, policy, or scheduling logic.

### 5. Readable State Before Optimized State

Where possible, prefer memory and runtime artifacts that a human can inspect directly before introducing optimized but opaque machinery.

### 6. A Phase Is Not Done Until It Is Explainable

Each phase should make it easy to answer:

- why the new boundary exists
- which new problem it solves
- what confusion would happen if the boundary did not exist

## Definition of Done for Each Phase

Each roadmap phase should be considered complete only when all of the following are true.

### Architecture Done

- the new boundary has a clear single responsibility
- old boundaries did not silently absorb the new problem
- out-of-scope items are explicitly listed

### Behavior Done

- there is a clear manual path that proves the new capability works

### Failure Semantics Done

- the design states what happens on failure
- the design states what state is preserved or not preserved

### Tests Done

- deterministic local logic introduced by the phase has automated test coverage

### Observability Done

- logs or inspection surfaces can explain the critical causal path for that phase

### Scope Done

- the phase remains narrow enough that the reason for the new abstraction is still legible

## Decision Summary

This roadmap chooses:

- learning-oriented sequencing over direct feature copying
- single-process single-machine implementation by default
- explicit future replacement boundaries without current distributed implementation
- readable persistent memory before advanced retrieval
- tool abstraction before plugin architecture
- plugin architecture before proactive behavior
- proactive behavior before drift/background self-work
- inspection and runtime stability after the product shape is already real

## Out of Scope for This Spec

This document does not define:

- implementation task breakdowns for individual phases
- exact file formats for future memory artifacts
- exact tool protocol schema
- exact plugin lifecycle API
- exact proactive scoring logic
- exact dashboard technology choices

Those should each be designed in their own follow-up spec when the corresponding roadmap phase becomes current.
