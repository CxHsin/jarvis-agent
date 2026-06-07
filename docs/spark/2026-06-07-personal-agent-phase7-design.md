# Personal Agent Phase 7 Design

## Goal

Define a Phase 7 proactive messaging system for `jarvis-agent` that introduces one new systemic problem only:

- how the agent should decide when to initiate contact instead of waiting for user input

This phase is intentionally narrow. It adds proactive scheduling and send-or-skip decision logic, while explicitly not introducing idle self-directed background work.

## Current Context

The current project already has the boundaries needed to justify a proactive phase:

- a passive Telegram message loop
- a core `AgentService` for passive reply generation
- long-term memory storage and memory policy
- a `ToolRegistry` and `ToolExecutor`
- a `PluginHost` with fixed lifecycle hooks

That means the next pressure is no longer how to reply, persist memory, invoke tools, or extend behavior through plugins. The next pressure is:

- how the system should periodically consider whether it ought to send a message first

The reference project `akashic-agent` contains a much richer proactive subsystem. This phase moves in that direction while keeping the design scoped to one learning-oriented boundary.

## Phase Scope

Phase 7 is explicitly defined as:

- `hybrid-lite`
- `plugin-signals`
- `single-target`
- `scheduler-owned decision`

That means:

- proactive candidates may come from both schedule-style plugins and signal-style plugins
- plugins provide structured proactive candidates rather than sending messages themselves
- the runtime sends only to one configured Telegram target in this phase
- the scheduler owns collection, filtering, dedupe, cooldown, and final send-or-skip control

This phase explicitly does not include:

- drift or idle background work
- multi-chat proactive delivery
- multi-channel proactive delivery
- scheduler-owned tool polling of external sources
- plugin-direct Telegram delivery
- complex retry, replay, or quota systems

## Problem Statement

Without a proactive boundary, every future "remind me", "notify me", or "push something interesting" capability would likely leak into the passive loop or into ad hoc plugin side effects.

That leads to several problems:

- the passive chat path stops being purely passive
- plugins become tempted to bypass core coordination and send directly
- cooldown, dedupe, and user-interruption rules become inconsistent
- it becomes hard to explain why the agent sent a message at a given time

Phase 7 therefore introduces a dedicated proactive scheduler whose job is to ask one narrow question on a loop:

- should the agent send the user a message right now

## Design Choice

This phase chooses a scheduler-owned proactive decision system.

It explicitly does not choose:

- plugin-owned send decisions
- scheduler-direct polling of tools and external systems
- a full `SignalSource` abstraction that supports multiple producer kinds at once
- a proactive-plus-drift combined runtime

The chosen design is deliberately narrow:

- plugins provide `ProactiveCandidate` values
- the scheduler periodically collects candidates from plugins
- the scheduler applies deterministic guardrails
- a judge decides whether the top candidate should be sent or skipped
- Telegram delivery happens through a fixed single-target send path
- every proactive outcome is logged for dedupe, cooldown, and inspection

This keeps the new problem focused on initiative and timing rather than also introducing a second source-integration architecture.

## Goals

- Add a distinct proactive runtime without contaminating the passive reply path.
- Allow plugins to propose proactive opportunities through a structured contract.
- Centralize cooldown, dedupe, interruption protection, and send-or-skip logic.
- Keep the runtime single-process and local-first.
- Preserve the existing plugin, memory, tool, and transport boundaries.

## Non-Goals

- drift or idle autonomous work
- multi-target or multi-channel delivery
- a generic event bus for proactive work
- external queueing systems
- plugin sandboxing or isolation
- LLM-first proactive orchestration
- automatic retries for failed deliveries
- plugin-driven direct delivery side effects

## Architecture

Phase 7 adds a separate `ProactiveScheduler`.

The system now has two parallel paths:

- passive path: `TelegramBot -> AgentService.generate_reply(...)`
- proactive path: `ProactiveScheduler -> PluginHost.collect_proactive_candidates(...) -> ProactiveJudge -> TelegramSender`

This separation is the main architectural point of the phase.

### `ProactiveScheduler`

`ProactiveScheduler` is responsible for:

- waking on a configured interval
- constructing the proactive decision context
- collecting candidate signals from plugins
- applying hard-rule filtering
- invoking the proactive judge
- dispatching delivery when the decision is `send`
- recording proactive outcomes in durable log state

`ProactiveScheduler` does not:

- reply to inbound user messages
- poll arbitrary tools directly
- perform drift tasks
- own Telegram long polling

### `PluginHost`

`PluginHost` gains a new proactive hook, such as:

- `collect_proactive_candidates(context)`

Plugins may:

- emit schedule-derived candidates
- emit external-signal-derived candidates
- attach evidence and suggested text

Plugins may not:

- send Telegram messages directly
- bypass scheduler cooldown or dedupe rules
- own the proactive loop
- write proactive log state directly

### `ProactiveJudge`

`ProactiveJudge` is responsible for one narrow decision:

- should one of the current candidates be sent now, or should this tick be skipped

The judge is intentionally not the scheduler. It does not control timing or persistence. It only evaluates the already-filtered candidate set.

### `TelegramSender`

Phase 7 uses a fixed Telegram delivery target from configuration:

- one channel
- one `chat_id`

This phase does not introduce a generic multi-channel delivery abstraction.

### `AgentService`

`AgentService` remains passive-turn-only. It should not absorb proactive polling or proactive delivery concerns.

## Components And Data Flow

The minimum Phase 7 component set is:

- `ProactiveScheduler`
- `ProactiveContext`
- `PluginHost` proactive candidate hook
- `ProactiveJudge`
- `ProactiveDeliveryLog`

The causal flow for one proactive tick should be fixed and legible.

### 1. Tick

`ProactiveScheduler` wakes on a configured interval.

### 2. Context Build

The scheduler builds a read-only `ProactiveContext` containing only the data needed for a proactive decision, such as:

- current time
- configured Telegram target `chat_id`
- last observed user-message timestamp
- last successful proactive-send timestamp
- bounded memory snapshot summary
- enabled plugin IDs

### 3. Candidate Collection

The scheduler calls:

- `PluginHost.collect_proactive_candidates(context)`

Each plugin returns zero or more `ProactiveCandidate` values.

### 4. Core Filtering

The scheduler applies deterministic filtering before any final decision:

- reject invalid candidates
- drop candidates whose `not_before` time has not arrived
- dedupe by recent `dedupe_key`
- enforce global cooldown
- enforce user-activity protection window
- cap the number of candidates that proceed further

### 5. Final Decision

The filtered candidates go to `ProactiveJudge`.

`ProactiveJudge` returns a `ProactiveDecision`:

- `send`
- `skip`
- a structured `reason`
- if `send`, the final message text and evidence summary

### 6. Delivery

If the result is `send`, the scheduler sends the message to the configured Telegram target.

### 7. Outcome Logging

Whether the result is `send`, `skip`, or delivery failure, the scheduler writes a proactive outcome record.

This logging boundary is separate from ordinary conversation history. Passive conversation history solves chat continuity. Proactive delivery logs solve dedupe, cooldown, and auditability.

## Candidate Contract

Phase 7 should define a small structured candidate contract. A `ProactiveCandidate` should include at least:

- `candidate_id`
- `plugin_id`
- `kind`
- `summary`
- `priority`
- `not_before`
- `dedupe_key`
- optional `suggested_message`
- optional `evidence`

The purpose of this type is not to encode a full workflow. It exists so the core can safely validate whether a plugin has proposed a meaningful proactive opportunity.

The governing rule is:

- plugins propose opportunities
- core decides whether and when to act on them

## Decision Policy

Phase 7 uses a two-step decision policy:

- deterministic hard rules first
- final single-candidate send-or-skip decision second

This keeps obvious no-send cases out of the deeper decision path.

### Hard Rules

Before the judge runs, the scheduler should enforce rules such as:

- if there are no candidates, skip
- if the global proactive cooldown is active, skip
- if the user has been active too recently, skip
- if a candidate's `not_before` is in the future, drop it
- if a candidate's `dedupe_key` was sent recently, drop it
- if too many candidates remain, keep only the highest-priority subset

### Final Selection

After hard filtering:

1. sort by `priority`
2. break ties by time-based ordering
3. choose at most one top candidate
4. return `send` or `skip`

Phase 7 should prefer:

- at most one proactive message per tick

This keeps user interruption pressure low and keeps state transitions easy to reason about.

### Judge Design

The first version of `ProactiveJudge` should be rule-first and replaceable later.

That means:

- simple proactive sends should work without requiring an LLM decision
- the boundary may later grow into a richer LLM-assisted judge if real pressure justifies it

Phase 7 does not require an LLM-first proactive engine.

### Structured Skip Reasons

`skip` must be explicit and structured. Recommended reasons include:

- `no_candidates`
- `cooldown_active`
- `user_recently_active`
- `duplicate_recent_send`
- `candidate_rejected`
- `judge_failed`

This makes proactive behavior explainable and prepares the project for later inspection surfaces.

## Failure Semantics And State

Phase 7 must degrade safely.

If the proactive system fails entirely, the following must still work:

- passive Telegram polling
- passive reply generation through `AgentService`
- memory reads and writes
- tool execution
- passive plugin hooks

### Candidate Collection Failure

If one plugin fails during candidate collection:

- drop that plugin's candidates for the current tick
- continue collecting from other plugins
- log plugin ID, hook name, time, and error summary

### Judge Failure

If the judge fails:

- treat the tick as `skip`
- do not send a fallback guess
- log the failure and candidate summary

### Delivery Failure

If Telegram delivery fails:

- record the result as `delivery_failed`
- do not mark it as a successful send
- do not automatically retry in Phase 7

Automatic retry is out of scope because it immediately introduces replay and idempotency concerns that belong to a later phase.

### State Write Failure

If the message sends successfully but proactive log state fails to persist:

- emit a high-visibility error log
- continue runtime execution

This is a meaningful degradation because later dedupe and cooldown decisions may become weaker. That risk is acceptable in this phase only if it is visible.

### Required State

Phase 7 should introduce only a small amount of proactive-specific state.

#### `ProactiveRuntimeState`

Ephemeral scheduler state, such as:

- last tick time
- last successful send time
- latest observed user-message time
- consecutive empty-tick count

#### `ProactiveDeliveryLog`

Durable outcome state for audit and dedupe:

- send attempt time
- `candidate_id`
- `dedupe_key`
- `plugin_id`
- target `chat_id`
- result: `sent | skipped | delivery_failed`
- structured `reason`

#### `ScheduleConfig`

Configuration for:

- tick interval
- global proactive cooldown
- recent-user-activity protection window
- candidate cap per tick
- maximum sends per tick

### User Activity Source

`last_user_message_at` should come from an explicit shared runtime state boundary, updated by the passive path or another core-owned runtime component.

The proactive system should not parse Telegram updates on its own just to infer user activity. Doing so would leak channel adapter concerns into scheduling logic.

## Testing Requirements

Automated tests should cover deterministic proactive boundary behavior, including:

- scheduler returns `skip` when there are no candidates
- multi-plugin candidate collection and merge behavior
- invalid candidates are rejected without breaking valid ones
- global proactive cooldown behavior
- recent-user-activity protection behavior
- `dedupe_key`-based duplicate suppression
- maximum one send per tick
- judge failure degrades to `skip`
- Telegram delivery failure becomes `delivery_failed`
- proactive-path exceptions do not break the passive reply path

The goal is not to test every reminder idea or signal plugin. The goal is correctness of the proactive boundary itself.

## Observability Requirements

Phase 7 should emit enough runtime information to explain a proactive tick end to end.

Minimum useful observability:

- scheduler startup and shutdown
- tick time and tick duration
- number of collected candidates
- number of candidates filtered by hard rules
- plugin IDs participating in the tick
- final decision: `send | skip`
- final reason
- delivery success or failure

Logs should avoid dumping full user history or large raw memory contents by default. Summaries and structured reasons are sufficient.

## Definition Of Done

Phase 7 is complete when all of the following are true:

- a distinct `ProactiveScheduler` exists and is independent from passive reply handling
- `PluginHost` exposes a proactive candidate collection hook
- plugins return `ProactiveCandidate` values instead of sending messages directly
- proactive delivery is fixed to one configured Telegram target
- the core enforces cooldown, recent-user-activity protection, dedupe, and one-send-per-tick rules
- proactive failures do not break the passive message loop
- a durable proactive delivery log exists for dedupe and audit
- deterministic proactive logic has automated tests
- logs can explain why the agent sent a proactive message or skipped a tick

## Out Of Scope For This Spec

This document does not define:

- drift or idle background-task execution
- multi-chat proactive routing
- multi-channel delivery abstraction
- direct tool polling by the proactive scheduler
- remote event-source integration contracts
- automatic delivery retry or idempotent replay
- an LLM-first proactive orchestration engine
- a distributed scheduler or worker system

Those belong to later phases only if real pressure justifies them.

## Decision Summary

Phase 7 should introduce a local proactive scheduler that:

- keeps passive and proactive paths separate
- treats plugins as proactive candidate providers
- centralizes cooldown, dedupe, and interruption rules
- sends only to one configured Telegram target
- logs every proactive outcome for explanation and future inspection
- degrades safely when proactive work fails

This is intentionally narrower than `akashic-agent`'s richer proactive runtime, but it introduces the right boundary for initiative without also importing drift, tool-driven polling, or multi-channel complexity into the same phase.
