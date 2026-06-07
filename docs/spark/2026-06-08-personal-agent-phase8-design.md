# Personal Agent Phase 8 Design

## Goal

Define a narrow Phase 8 drift system for `jarvis-agent` that introduces one new systemic problem only:

- how the agent should use idle time for low-priority background work without contaminating passive reply handling or proactive messaging

This phase is intentionally narrow. It does not attempt to build a general autonomous task system. It introduces a dedicated idle-work boundary whose only job is to decide whether one safe background task should run during a clearly idle window.

## Current Context

The current project already has the boundaries that justify a drift phase:

- a passive Telegram reply path through `TelegramBot` and `AgentService`
- long-term memory storage and a memory policy boundary
- a tool system with registration, execution, and tool-loop behavior
- a plugin system with fixed lifecycle hooks
- a proactive scheduler with candidate collection, cooldown, dedupe, and structured outcome logging

That means the next pressure is no longer how to reply, persist memory, invoke tools, extend behavior, or decide when to proactively contact the user.

The next distinct pressure is:

- when the system is idle, how it should safely do small background tasks without turning the runtime into a general task platform

The reference project `akashic-agent` contains a richer drift concept. This phase moves in that direction while preserving the roadmap rule that each phase introduces exactly one new systemic problem.

## Phase Scope

Phase 8 is explicitly defined as:

- `narrow`
- `idle-window-only`
- `single-task-per-tick`
- `plugin-proposed`
- `core-filtered`

That means:

- drift work is considered only during explicit idle windows
- plugins may propose drift tasks, but core runtime decides whether one task may run
- each drift tick executes at most one task
- drift remains separate from proactive messaging and passive reply handling
- drift tasks must be low-priority, bounded, and safe to skip

This phase explicitly does not include:

- a general autonomous planning loop
- multi-step task orchestration
- background retry queues
- multi-task scheduling pools
- cross-process workers
- plugin-direct infinite background loops
- LLM-driven task decomposition
- dashboard or operator UI

## Problem Statement

Without a dedicated drift boundary, every future background maintenance idea would tend to leak into the wrong place:

- the passive reply path could start doing opportunistic maintenance during user turns
- the proactive scheduler could become responsible for both initiative and background work
- plugins could start running ad hoc background side effects without shared guardrails
- observability would become unclear because "why did the agent do this just now" would have no single owner

Phase 8 therefore introduces one narrow runtime question:

- when the system appears idle, should it execute one low-priority background task right now

That is a different problem from:

- should the agent proactively contact the user

and it must remain a different boundary.

## Design Choice

This phase chooses a separate drift runtime:

- `DriftRunner`

It explicitly does not choose:

- embedding drift inside `ProactiveScheduler`
- allowing plugins to own their own background loops
- turning drift into a queue-backed orchestration system
- combining proactive and drift into a single generic scheduler abstraction

The chosen design is deliberately small:

- plugins propose `DriftTask` values
- `DriftRunner` periodically checks whether the system is idle enough to run background work
- deterministic hard rules decide whether any task is eligible
- at most one task executes per tick
- every drift outcome is recorded for dedupe, explanation, and later inspection

This keeps the new problem focused on safe idle work instead of also introducing a second-order scheduling platform.

## Goals

- Add a distinct drift runtime without contaminating passive or proactive paths.
- Let plugins propose bounded background work through a structured contract.
- Centralize idle gating, dedupe, concurrency protection, and execution outcome recording.
- Keep the runtime single-process and local-first.
- Preserve the current memory, tool, plugin, and transport boundaries.

## Non-Goals

- general autonomy
- task graphs or multi-step planning
- background work queues
- automatic retries
- plugin sandboxing
- process isolation
- dashboard surfaces
- cross-channel or cross-chat routing
- merging drift and proactive into one scheduler

## Architecture

Phase 8 adds a separate `DriftRunner`.

The runtime now has three parallel paths:

- passive path: `TelegramBot -> AgentService.generate_reply(...)`
- proactive path: `ProactiveScheduler -> collect candidates -> judge -> Telegram send`
- drift path: `DriftRunner -> collect drift tasks -> filter -> execute -> record outcome`

This separation is the main architectural point of the phase.

### `DriftRunner`

`DriftRunner` is responsible for:

- waking on a configured interval
- checking whether the runtime is currently idle enough for background work
- constructing drift decision context
- collecting drift tasks from plugins
- applying deterministic filtering rules
- selecting at most one task to execute
- executing the task and recording the result

`DriftRunner` does not:

- reply to inbound user messages
- decide whether to proactively contact the user
- own Telegram long polling
- become a general background task orchestrator

### `PluginHost`

`PluginHost` gains a dedicated drift hook, such as:

- `collect_drift_tasks(context)`

Plugins may:

- propose maintenance tasks
- propose pending-memory review tasks
- propose bounded low-priority workspace checks
- attach task metadata that allows core filtering and explanation

Plugins may not:

- start their own unmanaged infinite background loop
- bypass idle gating or dedupe rules
- directly redefine proactive scheduling semantics through drift
- turn drift hooks into a generic orchestration engine

### `ToolExecutor`

Drift tasks may reuse the existing tool boundary when needed, but Phase 8 does not redefine the tool system. Tools remain tools. Drift is a scheduling boundary around when low-priority work may happen.

### `AgentService`

`AgentService` remains passive-turn-only. It should not absorb background idle-work concerns.

### `ProactiveScheduler`

`ProactiveScheduler` remains initiative-only. It decides whether the agent should contact the user. It should not absorb background maintenance ownership.

## Components And Data Flow

The minimum Phase 8 component set is:

- `DriftRunner`
- `DriftContext`
- `PluginHost` drift task hook
- `DriftTask`
- `DriftExecutionLog`

The causal flow for one drift tick should be fixed and legible.

### 1. Tick

`DriftRunner` wakes on a configured interval.

### 2. Idle Check

The runner checks whether the runtime is sufficiently idle for background work.

### 3. Context Build

The runner builds a read-only `DriftContext` containing only the data needed for task selection, such as:

- current time
- last observed user-message timestamp
- last successful proactive-send timestamp
- memory snapshot or bounded summary
- available tools
- enabled plugin IDs

### 4. Task Collection

The runner calls:

- `PluginHost.collect_drift_tasks(context)`

Each plugin returns zero or more `DriftTask` values.

### 5. Core Filtering

The runner applies deterministic filtering before execution:

- reject invalid tasks
- skip all work if user activity is too recent
- skip all work if a proactive send happened too recently
- skip all work if another drift task is already running
- drop tasks whose `not_before` time has not arrived
- drop tasks whose `dedupe_key` ran recently
- drop tasks whose estimated cost exceeds the Phase 8 budget

### 6. Final Selection

From remaining tasks:

1. sort by `priority`
2. break ties by time and stable ID ordering
3. select at most one task

### 7. Execution

If a task is selected, the runner executes it once.

### 8. Outcome Logging

Whether the result is `executed`, `skipped`, or `failed`, the runner writes a structured outcome record.

## Drift Task Contract

Phase 8 should define a small structured task contract. A `DriftTask` should include at least:

- `task_id`
- `plugin_id`
- `kind`
- `summary`
- `priority`
- `not_before`
- `dedupe_key`
- `estimated_cost`
- `requires_tools`
- `execute(context) -> DriftOutcome`

The point of this type is not to encode a complete workflow engine. It exists so the core can safely answer:

- is this task eligible now
- is it small enough for this phase
- why did the system execute it

### Allowed Task Shape

Phase 8 drift tasks must be:

- low-priority
- bounded in runtime
- safe to skip
- safe to fail without breaking passive or proactive paths
- understandable from metadata without reading arbitrary plugin internals

### Recommended `kind` Values

Recommended narrow task categories include:

- `memory_maintenance`
- `pending_review`
- `history_cleanup`
- `light_workspace_check`

These are examples of acceptable narrow work classes, not a requirement to implement all of them immediately.

### Explicitly Disallowed Task Shape

This phase should not allow drift tasks that are:

- long-running blocking crawls
- infinite polling loops
- direct outward user-message delivery
- broad destructive workspace mutation
- multi-step autonomous planning chains

If a background idea requires those capabilities, it belongs to a later phase rather than this one.

## Idle Window Policy

Phase 8 should use a conservative idle-window policy.

Drift is not "run on every interval." It is:

- run on an interval
- check whether the system is idle enough
- only then consider whether one task may run

At minimum, the runner should require:

- no recent user message
- no recent successful proactive send
- no passive reply currently in progress
- no other drift task currently running

If required state is unavailable or ambiguous, the runner should degrade conservatively:

- treat the runtime as not idle enough
- skip the tick

The default risk posture for Phase 8 is:

- if uncertain, do not run drift work

### Configuration

Idle-window and drift behavior should use explicit config rather than piggybacking on proactive config semantics. Recommended fields include:

- `tick_interval_seconds`
- `idle_grace_seconds_after_user_message`
- `idle_grace_seconds_after_proactive_send`
- `max_task_runtime_seconds`
- `dedupe_window_seconds`

These settings overlap conceptually with proactive guardrails, but they solve a different problem:

- proactive avoids interrupting the user
- drift avoids competing with foreground runtime work

## Decision Policy

Phase 8 uses a rule-first execution policy:

- deterministic hard rules first
- single-task selection second
- best-effort execution third

This phase should prefer:

- at most one drift task per tick

That rule is what keeps the drift runner legible and prevents it from silently growing into a queue-based task runtime.

### Structured Skip Reasons

`skip` should be explicit and structured. Recommended reasons include:

- `no_tasks`
- `user_recently_active`
- `recent_proactive_send`
- `drift_already_running`
- `task_not_ready`
- `duplicate_recent_execution`
- `task_cost_too_high`
- `runtime_state_unavailable`

These reasons are important for logs and for later inspection surfaces.

## Failure Semantics And State

Phase 8 must degrade safely.

If the drift system fails entirely, the following must still work:

- passive Telegram polling
- passive reply generation
- memory reads and writes
- tool execution
- proactive scheduling
- passive and proactive plugin hooks

### Task Collection Failure

If one plugin fails during drift task collection:

- drop that plugin's tasks for the current tick
- continue collecting from other plugins
- log plugin ID, hook name, time, and error summary

### Task Execution Failure

If a drift task fails during execution:

- record the result as `failed`
- clear the in-flight task marker
- do not automatically retry
- continue future ticks normally

Automatic retry is out of scope because it immediately introduces replay and orchestration concerns that belong to a later phase.

### Log Write Failure

If drift outcome persistence fails:

- emit a high-visibility error log
- continue runtime execution

This is a meaningful degradation because dedupe and auditability may weaken, but that risk is acceptable in this phase if it is visible.

### Missing State Failure

If required idle-check state cannot be read reliably:

- skip the tick
- record a structured conservative reason such as `runtime_state_unavailable`

The runner must not optimistically execute work when the runtime cannot prove it is idle enough.

### Required State

Phase 8 should introduce only a small amount of drift-specific state.

#### `DriftRuntimeState`

Ephemeral process-local state, such as:

- `last_tick_at`
- `last_task_started_at`
- `last_task_finished_at`
- `last_successful_task_at`
- `currently_running_task_id`
- `consecutive_skip_count`

#### `DriftExecutionLog`

Durable outcome state for dedupe and audit:

- timestamp
- `task_id`
- `plugin_id`
- `kind`
- `dedupe_key`
- result: `executed | skipped | failed`
- structured reason
- optional duration summary

#### `DriftContext`

Read-only task selection context, such as:

- current time
- last user activity time
- last proactive-send time
- bounded memory snapshot
- available tools
- enabled plugin IDs

The key constraint is that plugins get only enough read-side context to propose tasks. They should not receive broad mutable access to runtime internals.

## Testing Requirements

Automated tests should cover deterministic drift-boundary behavior, including:

- runner returns `skip` when there are no drift tasks
- recent user activity suppresses drift execution
- recent proactive send suppresses drift execution
- an already-running drift task suppresses a second task
- `not_before` filtering
- `dedupe_key`-based duplicate suppression
- task-cost budget filtering
- multi-plugin task collection and stable single-task selection
- plugin collection failure does not break other plugins
- task execution failure records `failed`
- execution-log write failure does not break passive or proactive paths

The goal is not to test whether drift tasks are "smart." The goal is correctness of the drift boundary itself.

## Observability Requirements

Phase 8 should emit enough runtime information to explain a drift tick end to end.

Minimum useful observability:

- runner startup and shutdown
- tick time and tick duration
- number of collected tasks
- number of tasks filtered and reason categories
- final result: `executed | skipped | failed`
- final reason
- selected `task_id`, `plugin_id`, and `kind`
- optional execution duration summary

Logs should avoid dumping full memory contents, full chat transcripts, or raw large tool outputs by default. Structured summaries are sufficient.

These logs and execution records should be compatible with a later Phase 9 inspection surface, but Phase 8 itself does not require a dashboard.

## Definition Of Done

Phase 8 is complete when all of the following are true:

- a distinct `DriftRunner` exists and remains separate from `ProactiveScheduler`
- `PluginHost` exposes a dedicated drift task collection hook
- plugins provide structured `DriftTask` values rather than unmanaged background loops
- idle-window gating is explicit, conservative, and configurable
- each tick executes at most one drift task
- drift execution does not break passive reply handling
- drift execution does not absorb proactive send decisions
- a durable drift execution log exists for dedupe and audit
- failures degrade conservatively to `skip` or `failed`
- deterministic drift logic has automated test coverage
- logs can explain why a drift task executed or why the tick skipped

## Out Of Scope For This Spec

This document does not define:

- general autonomous planning
- multi-task queues
- automatic retries
- cross-process background workers
- LLM-first drift orchestration
- a unified proactive-plus-drift scheduler
- dashboard and admin interfaces
- plugin sandboxing

Those belong to later phases only if real pressure justifies them.

## Decision Summary

Phase 8 should introduce a narrow local `DriftRunner` that:

- runs only during explicit idle windows
- executes at most one low-priority bounded task per tick
- accepts plugin-proposed `DriftTask` values but keeps selection and execution guardrails in core
- records every execution and skip outcome for dedupe and explanation
- degrades conservatively when state is uncertain or execution fails
- preserves clear boundaries between passive reply handling, proactive messaging, and drift work

This is intentionally narrower than the richer drift behavior in `akashic-agent`, but it adds the right boundary for safe background work without also introducing a general autonomous runtime.
