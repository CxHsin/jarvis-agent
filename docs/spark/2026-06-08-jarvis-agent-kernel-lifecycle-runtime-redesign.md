# Jarvis Agent Kernel Lifecycle Runtime Redesign

## Goal

Define an aggressive architecture redesign for this repository using `kachofugetsu09/akashic-agent` as a structural reference, with the explicit goal of replacing the current passive-turn-centered design with a reusable agent platform built around:

- a shared `Kernel`
- a formal `Lifecycle`
- multiple `Runtime` implementations

The target is not feature parity with `akashic-agent`, and it is not a cosmetic refactor. The target is to make this project capable of supporting passive chat, proactive messaging, and drift work as first-class execution modes that share one core turn engine.

## Confirmed Direction

The design below reflects the validated decisions from the review conversation:

- optimization focus: architecture refactor
- change level: aggressive redesign
- first-phase capabilities that must remain available:
  - Telegram passive conversation
  - basic memory
  - tool calling
  - plugins
  - proactive messaging
  - drift
  - current configuration flow
  - runnable test suite

## Scope

This design covers:

- target top-level architecture
- module boundaries
- shared turn execution model
- lifecycle and plugin redesign
- memory subsystem redesign
- runtime decomposition
- migration order
- compatibility and test strategy

This design does not cover:

- implementation of vector retrieval in phase one
- dashboard UI design
- multi-channel feature expansion beyond defining boundaries
- model-provider optimization details
- product-level persona or prompt wording work

## Current Project Assessment

The current repository already contains the right early subsystems:

- Telegram channel entry
- per-chat conversation storage
- file-based memory workspace
- consolidation and memory policy
- tool registry, executor, and tool loop
- plugin loading and hook execution
- proactive scheduler
- drift runner

The architectural problem is not missing subsystems. The problem is that the system is still centered on a passive reply path, with other capabilities attached around it.

Observed characteristics of the current design:

- `app/agent.py` is still the main user-facing center of execution
- `app/turns/` improves passive orchestration, but passive turn flow is still the dominant shape of the application
- `app/proactive.py` and `app/drift.py` have separate runtime logic and state conventions
- `app/plugins/types.py` already exposes many hook contexts, but the extension model is still a fixed hook list rather than a true lifecycle runtime
- `app/memory_store.py` and related files provide a solid markdown memory layer, but memory is not yet presented as a standalone subsystem with a facade

This means the repository has grown beyond a toy bot, but its architecture is still organized like one.

## Reference Project Analysis: `akashic-agent`

`akashic-agent` is larger and more mature, but the relevant lessons are architectural.

### What Matters Most

The strongest ideas worth borrowing are:

- the core agent logic is not owned by a transport-facing service
- passive, proactive, and drift paths are treated as distinct runtimes
- lifecycle phases are explicit and extensible
- plugins attach to lifecycle and tool boundaries rather than forcing more branching into the core
- memory is treated as a subsystem with multiple layers
- runtime state, delivery state, and memory maintenance are not all mixed into one turn path

### What Should Not Be Copied Directly

The following should be treated as later-stage inspiration, not phase-one requirements:

- full event-heavy platform behavior everywhere
- broad MCP source integration
- dashboard-first memory administration
- the complete slot ecosystem as-is
- every advanced plugin surface from the reference

The right move is to absorb the reference project's boundary design, not its full current feature surface.

## Core Design Decision

Adopt a three-layer architecture:

- `Kernel`
- `Lifecycle`
- `Runtime`

This is the recommended middle path between two weaker alternatives:

- weaker than needed: only splitting the current pipeline into more helper files
- heavier than useful: immediately turning the whole project into an event-bus microkernel

`Kernel + Lifecycle + Runtime` is the right target because it solves the current centralization problem without forcing every behavior into asynchronous event machinery on day one.

## Target Architecture

### Layer 1: Kernel

The `Kernel` is the shared turn engine. It owns the execution of a single turn from request to structured result.

The kernel is responsible for:

- accepting a `TurnRequest`
- preparing turn-scoped state
- running lifecycle phases
- rendering the final prompt
- running model reasoning and tool steps
- producing a structured `TurnResult`
- exposing commit-ready outputs, not transport-specific side effects

The kernel is not responsible for:

- Telegram I/O
- scheduler loops
- proactive source polling
- drift tick timing
- memory file formats
- delivery retries

### Layer 2: Lifecycle

The `Lifecycle` is the extension plane around the kernel.

It is responsible for:

- phase definitions
- phase module ordering
- plugin attachment points
- event fanout
- tool interception
- prompt block injection
- turn-side side-effect surfaces

The lifecycle is the answer to future complexity. New behavior should attach here instead of adding branches inside the kernel.

### Layer 3: Runtime

The `Runtime` layer adapts real execution environments to the kernel.

It is responsible for:

- building runtime-specific turn requests
- loading runtime activity state
- dispatching kernel output
- interacting with channels
- coordinating schedulers
- gating proactive and drift work

Passive, proactive, and drift should all be peer runtimes, not one primary runtime plus extras.

## Recommended Module Boundaries

Recommended package shape:

- `agent/kernel/`
  - `turn_request.py`
  - `turn_result.py`
  - `turn_kernel.py`
  - `reasoning_runner.py`
  - `prompt_renderer.py`
  - `tool_runtime.py`
- `agent/lifecycle/`
  - `phase.py`
  - `frame.py`
  - `modules.py`
  - `events.py`
  - `plugin_manager.py`
  - `tool_hooks.py`
- `agent/runtime/`
  - `passive/`
  - `proactive/`
  - `drift/`
  - `scheduler/`
  - `activity_state.py`
  - `outbound.py`
- `agent/memory/`
  - `facade.py`
  - `markdown_store.py`
  - `retrieval.py`
  - `consolidation.py`
  - `optimizer.py`
  - `types.py`
- `agent/channels/`
  - `telegram.py`
  - `base.py`
- `agent/bootstrap/`
  - `config_loader.py`
  - `setup_wizard.py`
  - `wiring.py`
- `agent/observe/`
  - `trace.py`
  - `metrics.py`
  - `audit.py`

The exact directory names can be adjusted, but the boundary intent should remain.

## Shared Turn Model

The core redesign hinges on one rule:

`passive`, `proactive`, and `drift` must share one `TurnKernel`.

They should differ in request construction and output policy, not in core execution semantics.

### Turn Request Types

Define at least three request types:

- `PassiveTurnRequest`
- `ProactiveTurnRequest`
- `DriftTurnRequest`

All should satisfy a common `TurnRequest` protocol with fields such as:

- `turn_kind`
- `session_id`
- `chat_id`
- `user_visible_input`
- `memory_policy_name`
- `available_tools`
- `runtime_metadata`
- `dispatch_policy`

### Shared Turn Pipeline

Every request should pass through the same high-level pipeline:

1. `Prepare`
2. `BeforeTurn`
3. `BeforeReasoning`
4. `PromptRender`
5. `BeforeStep`
6. `ReasoningStep`
7. `AfterStep`
8. `AfterReasoning`
9. `AfterTurn`
10. `Commit`
11. `Dispatch`

`Dispatch` may become a no-op for silent drift outcomes, but it should remain a formal step in the runtime flow.

### Structured Turn Result

The kernel should produce a `TurnResult`, not a raw reply string.

Suggested fields:

- `decision`
- `reply_text`
- `outbound_payload`
- `memory_delta`
- `tool_trace`
- `turn_notes`
- `artifacts`
- `side_effects`
- `telemetry`
- `commit_plan`

This creates a stable contract between kernel and runtime.

## Passive Proactive Drift Interaction Model

### Passive Runtime

The passive runtime should:

- receive inbound channel messages
- build a `PassiveTurnRequest`
- execute the kernel
- dispatch reply output to the outbound adapter
- update shared activity state

It should not own prompt assembly, memory file logic, or tool governance.

### Proactive Runtime

The proactive runtime should:

- wake on scheduler ticks
- gather source data through a source gateway
- build a `ProactiveTurnRequest`
- run the kernel
- dispatch proactive messages or record skip decisions

The proactive runtime should not embed its own special-purpose reasoning engine. It should rely on the shared kernel.

### Drift Runtime

The drift runtime should:

- wake only when idle policy allows
- construct a `DriftTurnRequest`
- run the kernel for silent or optional outbound work
- record artifacts and execution outcomes

Drift should not remain permanently limited to a direct "task.execute(context)" shape. Phase one may preserve lightweight tasks internally, but the target architecture should allow drift to be a full turn mode.

## Lifecycle Redesign

The current plugin model is already useful, but it is still based on a fixed list of hook fields. That will not scale through an aggressive redesign.

The replacement should formalize four extension surfaces.

### 1. Phase Modules

This is the primary extension surface.

Each lifecycle phase should be represented as a module chain. Modules declare:

- `slot`
- `requires`
- `produces`

The framework performs topology ordering. This allows plugins and built-ins to compose without relying on hard-coded if-else injection points.

### 2. Event Bus

The event bus should be used for observation and side-channel reactions.

Examples:

- `TurnStarted`
- `PromptRendered`
- `ToolCalled`
- `ToolReturned`
- `TurnCommitted`
- `DeliveryCompleted`
- `DriftExecuted`

Important rule:

- event handlers observe and fan out
- phase modules mutate core turn state

This avoids two competing control systems.

### 3. Tool Hooks

Add formal pre/post tool interception:

- `on_tool_pre`
- `on_tool_post`

Use cases:

- deny unsafe commands
- rewrite tool arguments
- enforce loop protection
- attach telemetry
- normalize result payloads

Tool control should not live only inside the tool loop implementation.

### 4. Tool Registration

Plugins may still register tools, but tool registration should become just one plugin capability, not the center of the plugin system.

## Recommended Lifecycle Phases

The lifecycle should define these phases explicitly:

- `BeforeTurn`
- `BeforeReasoning`
- `PromptRender`
- `BeforeStep`
- `AfterStep`
- `AfterReasoning`
- `AfterTurn`

### Phase Responsibilities

`BeforeTurn`

- acquire session and runtime context
- perform command interception
- allow hard abort before expensive work

`BeforeReasoning`

- finalize reasoning-visible context
- synchronize tools
- add reasoning hints

`PromptRender`

- assemble system prompt blocks
- render memory sections
- render runtime-specific sections

`BeforeStep`

- prepare each model/tool iteration
- apply early-stop policies

`AfterStep`

- collect step trace
- evaluate repetition or pressure conditions

`AfterReasoning`

- clean reply text
- extract citations or metadata
- build outbound payload
- build memory delta candidates

`AfterTurn`

- finalize commit extras
- emit post-turn events
- schedule follow-up work

## Memory Subsystem Redesign

The project already points in the right direction with markdown memory files. The redesign should preserve that layer while turning memory into an explicit subsystem.

### Design Principle

Memory is not a collection of files. Memory is a protocol-backed subsystem.

### Recommended Memory Layers

#### 1. Memory Facade

The kernel should depend only on a memory facade.

Suggested facade operations:

- `prepare_context()`
- `retrieve()`
- `retrieve_explicit()`
- `remember()`
- `forget()`
- `consolidate()`
- `optimize()`
- `load_runtime_snapshot()`
- `apply_delta()`

This separates kernel code from specific markdown file operations.

#### 2. Structured Markdown Layer

Preserve and formalize the current human-readable layer:

- `SELF.md`
- `MEMORY.md`
- `RECENT_CONTEXT.md`
- `PENDING.md`
- `HISTORY.md`

Role definitions:

- `SELF.md`: stable self-model and relationship stance
- `MEMORY.md`: durable long-term facts
- `RECENT_CONTEXT.md`: compressed recent context
- `PENDING.md`: buffered candidate facts waiting for optimization
- `HISTORY.md`: append-only timeline

#### 3. Retrieval Layer

Even if semantic retrieval is not fully implemented in phase one, the retrieval layer must exist as a boundary.

It should be able to grow toward:

- keyword retrieval
- time-window retrieval
- explicit recall
- interest retrieval for proactive selection

That interface should be introduced before any vector engine is added.

#### 4. Maintenance Layer

This layer owns:

- consolidation
- recent-context compression
- optimizer work
- dedupe
- recovery from interrupted maintenance

It should not be embedded directly inside the passive turn path.

### Memory Delta Contract

Do not let upper layers directly append raw text to memory files.

The kernel should emit a structured `MemoryDelta`, for example:

- `history_entries`
- `pending_items`
- `recent_context_update`
- `self_model_updates`
- `forget_operations`

The memory facade applies that delta to the underlying stores.

This preserves future storage flexibility.

## State Model Redesign

The current system already has conversation state, memory state, proactive state, and drift state. They need to be reorganized into a consistent state model.

### Turn State

Scope: one turn only.

Examples:

- request
- rendered prompt blocks
- tool step trace
- provisional reply
- transient artifacts

### Session State

Scope: one chat or logical session.

Examples:

- conversation history
- last user message time
- last assistant reply time
- cooldown markers
- abort markers
- last proactive send time

### Workspace State

Scope: cross-turn and cross-runtime.

Examples:

- memory markdown files
- delivery logs
- drift execution logs
- plugin key-value state
- scheduler persistence

### Required Storage Boundaries

Introduce explicit storage roles:

- `ConversationStore`
- `MemoryStore`
- `RuntimeStateStore`
- `ArtifactStore`

This prevents runtime-specific files from becoming ad hoc hidden dependencies.

## Runtime Layer Redesign

The runtime layer should translate real-world triggers into kernel requests.

### Passive Runtime Boundary

Responsibilities:

- inbound message acceptance
- session lookup
- passive request construction
- kernel invocation
- outbound reply dispatch
- activity recording

### Proactive Runtime Boundary

Responsibilities:

- scheduler-driven wake-up
- source gateway invocation
- proactive request construction
- result dispatch or skip recording
- delivery logging

### Drift Runtime Boundary

Responsibilities:

- idle gating
- drift request construction
- kernel invocation
- artifact and execution logging

### Channel Boundary

Telegram should be wrapped behind channel protocols covering:

- inbound adaptation
- outbound dispatch
- delivery result reporting
- identity mapping

### Scheduler Boundary

Unify all self-waking work into one scheduler service.

It should manage:

- periodic ticks
- cron-like schedules
- cooldown-aware delays
- activity-aware gating
- registered background jobs

This should eventually cover:

- proactive ticks
- drift ticks
- memory optimizer jobs
- scheduled reminders

## Source Gateway For Proactive

Even without MCP in phase one, proactive should gain a formal source boundary.

Suggested source protocols:

- `AlertSource`
- `ContentSource`
- `ContextSource`

That allows future integrations such as:

- RSS
- weather
- calendar
- GitHub
- email
- local sensors

without rewriting proactive runtime internals.

## Error Handling And Failure Semantics

The redesign should make failure boundaries explicit.

### Before Reply Exists

If request preparation, prompt rendering, or reasoning fails before a reply exists:

- no outbound message is sent
- no turn commit occurs
- no memory delta is applied
- failure is logged and surfaced to the runtime

### After Reply Exists But Before Dispatch

If reasoning succeeds but commit preparation fails:

- the runtime receives a structured failure result
- reply delivery policy can decide whether to send a fallback or drop the turn
- commit failure and reasoning success are logged separately

### After Dispatch Starts

If outbound delivery fails after a result is otherwise committed:

- commit state must remain durable
- delivery outcome must be recorded independently
- retry policy belongs to runtime or outbound dispatcher, not kernel

### Plugin And Hook Failures

Plugin failures should be isolated by extension surface:

- phase-module failure may abort the turn if the module is mandatory
- event-handler failure should not invalidate an already successful turn
- tool-hook failure should fail closed for safety-sensitive tools and fail open for observational hooks

Those policies must be explicit per hook type.

## Testing Strategy

The redesign should improve the test structure, not just the code structure.

### Required Test Layers

- kernel contract tests
- lifecycle ordering tests
- tool governance tests
- memory facade tests
- runtime integration tests
- scheduler policy tests
- outbound failure tests
- legacy parity tests for phase-one preserved behavior

### Key Assertions

- passive, proactive, and drift all invoke the same kernel contract
- lifecycle ordering is deterministic
- tool hooks can deny, rewrite, and observe calls correctly
- memory delta application is separate from turn reasoning
- delivery success and commit success are not conflated

## Recommended Migration Plan

Do not rewrite the repository in one cutover.

Adopt a staged migration that builds the new architecture in parallel with the old one.

### Stage 1: Foundation

Create the new architecture skeleton and contracts:

- `TurnRequest`
- `TurnResult`
- `TurnKernel`
- lifecycle phase contracts
- plugin manager abstraction
- memory facade
- outbound dispatcher
- runtime state protocols

No feature parity is required yet. The outcome is a stable empty architecture.

### Stage 2: Passive First

Migrate passive conversation to the new runtime first.

Why first:

- passive chat is the current backbone
- it validates the kernel contract
- proactive and drift both depend on the same turn semantics later

The new passive runtime should preserve user-visible behavior while moving execution onto the kernel and lifecycle.

### Stage 3: Plugin And Tool Governance

Migrate the current plugin and tool extension model into:

- phase modules
- event handlers
- tool hooks
- tool registration through plugin capabilities

Do not try to redesign every plugin before the lifecycle exists.

### Stage 4: Memory Subsystem

Move memory integration behind the memory facade and maintenance layer.

Success criteria:

- kernel depends only on the facade
- runtime code does not manipulate memory files directly
- consolidation and optimization can be tested independently

### Stage 5: Proactive And Drift

Migrate proactive and drift onto the shared kernel only after passive, lifecycle, and memory boundaries are stable.

Success criteria:

- proactive builds requests rather than owning special reasoning flow
- drift uses the same core turn contract
- both runtimes share activity state and outbound infrastructure

## Compatibility Strategy

Phase one compatibility should prioritize user-facing continuity, not internal API preservation.

Must remain operational:

- Telegram passive conversation
- basic memory behavior
- tool calling
- plugin support
- proactive messaging
- drift execution
- current config bootstrap flow
- runnable tests

Allowed to change:

- internal package layout
- plugin implementation details
- storage adapters behind the facade
- runtime composition

Temporary compatibility shims are acceptable if they are explicitly transitional.

## Why This Path Is Better Than The Alternatives

### Better Than Pipeline-Only Refactor

A pipeline-only refactor would improve file shape but preserve the current architectural hierarchy where passive turn flow remains the center and other runtimes remain secondary.

That would not solve the next-order problem.

### Better Than Event-Bus-First Microkernel

A fully event-driven microkernel would introduce too much control-plane complexity too early.

This repository needs stable core contracts first:

- shared turn execution
- lifecycle extension
- memory facade
- runtime separation

Those contracts should exist before broader event-platform ambitions.

## Risks And Mitigations

### Risk: Scope Explosion

Because this is an aggressive redesign, there is a real risk of mixing architecture work with feature work.

Mitigation:

- hold phase one to preserved capabilities only
- defer dashboard, vector retrieval implementation, and broad channel expansion

### Risk: Long Dual-Stack Period

Parallel old/new paths can become expensive.

Mitigation:

- migrate passive first
- define exit criteria per stage
- remove legacy code after parity checkpoints rather than keeping both indefinitely

### Risk: Plugin Migration Friction

Moving from fixed hooks to lifecycle modules may break plugin assumptions.

Mitigation:

- add a short-lived adapter layer
- migrate built-in plugins first
- define test fixtures for plugin ordering and failure semantics

## Definition Of Done

This redesign should be considered successful only when all of the following are true.

### Architecture Done

- passive, proactive, and drift all run through a shared kernel contract
- lifecycle is the formal extension plane
- runtime responsibilities are separated from kernel responsibilities
- memory exists behind a facade and maintenance layer

### Behavior Done

- first-phase required capabilities remain operational
- passive chat behavior remains usable through Telegram
- proactive and drift continue to function through the new runtime boundaries

### Engineering Done

- new tests cover kernel, lifecycle, memory facade, and runtime integration
- delivery and commit semantics are independently observable
- core modules no longer depend on Telegram-specific logic

### Scope Done

- phase one stops after architecture migration and preserved capability parity
- optional advanced work remains explicitly out of scope until after the redesign stabilizes

## Decision Summary

This design chooses:

- `Kernel + Lifecycle + Runtime` over shallow pipeline cleanup
- one shared turn engine over runtime-specific orchestration stacks
- lifecycle modules over expanding fixed hook lists
- memory facade plus layered memory over direct file-centric integration
- runtime peers over passive-turn centralization
- staged migration over full rewrite cutover
