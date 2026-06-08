# Jarvis Runtime Architecture Design

## Overview

This spec defines the long-term runtime architecture target for `jarvis-agent` after the MVP and initial tool framework work.

The purpose of this phase is not to fully implement a new runtime immediately.

The purpose is to establish a stable architecture baseline for future infrastructure work so that:

- passive reply handling can become reliable
- proactive behavior can be added without contorting the passive loop
- drift-style background behavior can be introduced without turning the runtime into one oversized pipeline
- shared infrastructure can evolve without binding every capability to one execution model

This design follows the same high-level direction as `akashic-agent`:

- the system is centered on explicit pipelines
- the event bus is an extension and observation layer, not the primary state engine
- multiple runtime modes may coexist without being forced into one abstract super-pipeline

## Product Goal

This phase is successful when Jarvis has a clear runtime architecture target in which:

- shared runtime contracts are explicit
- passive, proactive, and drift execution paths have clean boundaries
- infrastructure modules have stable dependency direction
- future work can be implemented incrementally without re-deciding the top-level architecture each time

## Why This Direction

The current project is still structurally close to an MVP:

- `app/main.py` wires concrete dependencies directly
- `Agent` owns the passive turn loop end to end
- tool execution already has a stronger subsystem boundary
- session storage is still a narrow local implementation
- there is no runtime-level contract for future proactive or drift behavior

If the codebase continues to grow from the current shape, the likely failure mode is not one bad module.

The likely failure mode is architectural drift:

- lifecycle logic spreads across app bootstrap, agent loop, and future background tasks
- proactive behavior gets added as conditionals around the passive path
- drift becomes an awkward extension of either passive or proactive logic
- shared services become coupled to one concrete execution path

The goal of this spec is to stop that drift before larger features are built.

## Scope

### In Scope

- long-term runtime architecture target
- runtime contracts shared across execution modes
- boundaries between passive, proactive, and drift flows
- package-level module decomposition
- dependency direction rules
- phased migration path from the current codebase

### Out of Scope

- full implementation of proactive behavior
- full implementation of drift behavior
- final memory architecture
- plugin marketplace or packaging
- MCP runtime
- multi-process or distributed execution

## Current Baseline

Today the runtime is effectively:

`Telegram -> Agent.run_turn -> LLM/tool loop -> session append -> reply`

This shape is acceptable for the MVP, but it has two structural limits.

First, the passive path is the only real execution model.

Second, infrastructure boundaries are uneven:

- tooling is starting to become a subsystem
- runtime orchestration is still embedded in one concrete agent loop

The existing tool framework work is still compatible with this spec.

In fact, it becomes one of the first shared services under the new runtime architecture.

## Recommended Approach

Three directions were considered.

1. continue expanding the current passive agent loop and add future modes around it
2. build one abstract super-pipeline and force passive, proactive, and drift into it
3. define shared runtime contracts while preserving multiple mode-specific pipelines

The recommended direction is `3`.

Reasoning:

- it matches the actual behavioral differences between passive, proactive, and drift flows
- it preserves the strongest lesson from `akashic-agent`: pipelines carry semantics, event hooks carry extensibility
- it avoids fake unification that would increase complexity instead of reducing it
- it gives Jarvis a stable infrastructure target without requiring a full rewrite up front

## Architecture Baseline

Jarvis should adopt this long-term runtime shape:

`shared runtime contract + multiple pipelines`

That means:

- there is one runtime foundation for shared orchestration boundaries
- there is not one universal business pipeline
- passive, proactive, and drift remain distinct execution modes

The runtime layer governs:

- how execution modes are entered
- how shared services are accessed
- how lifecycle events are emitted
- how plugins or modules attach
- how concurrency guards are checked

The pipelines govern:

- what each mode does
- how its stages are ordered
- how it decides success or failure
- how it commits outputs or side effects

This is the central architectural rule of the spec:

`runtime contract governs orchestration boundaries; pipelines own behavioral semantics`

## Shared Runtime Contract

The shared runtime contract should remain narrow and composable.

It should not become a disguised global state machine.

### 1. Runtime Entrypoints

The runtime should define explicit entrypoints for external triggers.

Examples:

- inbound channel message
- scheduler tick
- system-triggered background wakeup

These entrypoints should only:

- normalize the trigger
- identify the target execution mode
- construct mode input context
- hand off to the corresponding pipeline runner

They should not embed business decisions that belong to a pipeline.

### 2. Event Bus

Jarvis should include an event bus, but use it in the same restrained role seen in `akashic-agent`.

The event bus is:

- a lifecycle hook layer
- an observation layer
- a controlled extension layer

The event bus is not:

- the primary execution engine
- the sole source of runtime truth
- an event-sourcing backbone

Pipelines still advance themselves directly.

The event bus exists so that modules, plugins, logging, and future instrumentation can observe or lightly influence lifecycle transitions without owning the main control flow.

### 3. Processing State

Jarvis should keep `ProcessingState` intentionally narrow.

Following the `akashic-agent` model, it should begin as a session-scoped passive-busy guard, not a full runtime state model.

Its first responsibility is:

- passive pipeline marks a session as busy when handling an inbound turn
- proactive pipeline checks that session before attempting outbound delivery

This prevents passive reply work and proactive delivery from colliding in the same session.

If additional coordination is needed later, it should be added through separate components rather than inflating `ProcessingState` into a system-wide state machine.

### 4. Shared Service Interfaces

All pipelines should depend on shared service interfaces rather than directly on one another.

These shared services should include at least:

- tool runtime
- state and memory store interfaces
- LLM provider interface
- channel adapter interface
- scheduler or clock interface
- policy guard interface
- artifact and log sink interfaces

The current tool framework naturally fits this layer.

Session storage will need to evolve into a broader state service boundary later, but it should still live here conceptually.

### 5. Hook and Module Attachment

Extensions should attach at explicit lifecycle points, not by importing and editing pipeline internals.

Hook points should exist around mode-appropriate lifecycle steps, for example:

- before context assembly
- after context assembly
- before model call
- after tool execution
- before delivery commit
- after delivery commit

The exact hook set may differ by pipeline, but the attachment model should stay consistent.

## Pipeline Model

Passive, proactive, and drift should not be forced into one execution pipeline.

They should be modeled as separate runners that share runtime contracts.

### Passive Pipeline

The passive pipeline is the primary user-facing path.

Its job is to take an inbound user message and complete one bounded response turn.

It owns:

- inbound message normalization after channel adaptation
- session context assembly
- model call and tool loop
- final response generation
- session commit and outbound reply submission

This path should remain the most deterministic and best-tested runtime path.

### Proactive Pipeline

The proactive pipeline is a scheduled active-delivery runner.

Its job is not to respond to inbound messages.

Its job is to decide whether the system should proactively send a message to a user or session.

It should own stages such as:

- candidate gathering
- signal evaluation
- value judgment
- content generation
- delivery decision
- outbound commit

It should check `ProcessingState` before delivery to avoid colliding with an active passive turn in the same session.

### Drift Pipeline

Following the `akashic-agent` model, drift should not be treated as a fully independent top-level interaction path from day one.

Instead, drift should be modeled as a fallback background runner entered from the proactive mode when no normal proactive delivery is produced.

In that shape:

- proactive remains the scheduled outer loop
- drift becomes a controlled idle-budget runner
- drift work is driven by skill or task definitions rather than by the passive reply loop

This keeps drift from turning into an orphaned third architecture inside the same runtime.

### Relationship Between Modes

The relationship should be:

- passive handles explicit inbound interaction
- proactive handles periodic outbound decision-making
- drift handles fallback background work when proactive has nothing worth delivering

Cross-mode interaction should happen only through:

- shared services
- explicit runtime events
- explicit candidate or result objects

Pipelines should not reach into one another's internal stages.

## Module Decomposition

The long-term package structure should evolve toward this shape.

### `jarvis/runtime/`

Shared runtime contracts and bootstrap components.

Suggested responsibilities:

- runtime entrypoints
- event bus
- processing state
- shared runtime context objects
- pipeline runner interfaces
- hook dispatch
- application bootstrap and dependency assembly

### `jarvis/pipelines/passive/`

Passive turn pipeline only.

Suggested responsibilities:

- inbound turn runner
- context assembly
- model/tool loop
- reply commit

### `jarvis/pipelines/proactive/`

Scheduled proactive pipeline.

Suggested responsibilities:

- proactive wakeup loop
- candidate evaluation
- decision logic
- outbound proactive commit
- drift fallback entry

### `jarvis/pipelines/drift/`

Drift runner and supporting skill execution logic.

Suggested responsibilities:

- drift task loading
- drift runner
- drift completion policy
- drift result handling

This package remains conceptually adjacent to proactive even if it is stored separately.

### `jarvis/services/`

Shared capabilities consumed by all pipelines.

Suggested responsibilities:

- tool runtime
- state and memory services
- provider adapters
- policy services
- scheduling abstractions
- artifact and logging sinks

### `jarvis/channels/`

External transport adapters only.

Suggested responsibilities:

- receive external updates
- normalize transport payloads
- submit runtime entry requests
- deliver outbound payloads

Channels should not orchestrate business flow.

## Dependency Direction

The dependency direction should be fixed as:

`channels / timers / triggers -> runtime -> pipeline -> services -> providers / storage`

The following reverse dependencies should be treated as invalid architecture:

- one pipeline importing another pipeline's internal stage logic
- services depending on pipelines
- channels calling tools, state stores, or providers directly for business logic
- plugins mutating runtime internals outside explicit hooks or contracts

These rules matter more than exact directory names.

The goal is to preserve a stable mental model for future work.

## Migration of Current Code

The current code maps into the target architecture like this.

### Existing Code That Already Fits the Direction

- `jarvis/tools/` is already evolving into a shared service subsystem
- `jarvis/providers/llm.py` is already a service-side adapter
- `jarvis/channels/telegram.py` is already transport-facing

### Existing Code That Needs Repositioning

- `jarvis/core/agent.py` currently mixes passive pipeline semantics with runtime orchestration role
- `app/main.py` currently performs direct application assembly that should eventually move behind runtime bootstrap boundaries
- `jarvis/state/session_store.py` is still a narrow implementation rather than a broader state service contract

### Intended Reframing

The current `Agent` should evolve toward a passive pipeline runner rather than remain the universal runtime coordinator.

The current bootstrap path should evolve from:

`app/main.py -> build dependencies -> Agent`

toward:

`app/main.py -> runtime bootstrap -> runtime entrypoints + pipelines + shared services`

## Phase Plan

This spec defines a long-term target, but the implementation should remain incremental.

### Phase 1: Runtime Skeleton

Goals:

- introduce `jarvis/runtime/` as a shared runtime boundary
- move application assembly toward runtime bootstrap
- define runtime entrypoint contracts
- add a minimal event bus contract
- add a minimal `ProcessingState` with passive-busy semantics only

This phase should not yet implement proactive or drift behavior fully.

### Phase 2: Passive Pipeline Extraction

Goals:

- reframe `jarvis/core/agent.py` as passive pipeline logic
- split passive-specific orchestration from generic runtime concerns
- make passive pipeline consume shared service interfaces explicitly

This stabilizes the main path before any background capability is added.

### Phase 3: Service Boundary Cleanup

Goals:

- move tool runtime cleanly under shared services
- widen session storage toward a more general state service interface
- formalize provider and channel contracts where needed

This phase makes future pipelines depend on stable capabilities rather than ad hoc implementations.

### Phase 4: Proactive Runner Introduction

Goals:

- add scheduler-driven proactive entry
- add session busy checks through `ProcessingState`
- implement one bounded proactive delivery path

This is the first point where multi-mode runtime behavior becomes real.

### Phase 5: Drift Fallback Runner

Goals:

- introduce drift as proactive fallback background execution
- keep drift isolated from passive pipeline internals
- define its task or skill loading boundary

This preserves the intended akashic-style relationship between proactive and drift.

## Acceptance Criteria

This architecture phase is complete when all of the following are true:

1. Jarvis has an agreed top-level runtime shape based on shared runtime contracts and multiple pipelines.
2. The passive path is clearly identified as one pipeline, not the permanent home for all orchestration.
3. Event bus usage is explicitly limited to lifecycle extension and observation rather than promoted into the main execution engine.
4. `ProcessingState` is explicitly scoped as a narrow passive-busy guard rather than a global runtime state machine.
5. Proactive and drift are defined with a relationship compatible with the `akashic-agent` model.
6. Package boundaries and dependency direction are explicit enough to guide future implementation work.
7. The first implementation phase is small enough to begin without rewriting the whole project.

## Non-Goals for This Spec

This spec does not define:

- final prompts for passive, proactive, or drift modes
- final memory retrieval behavior
- final plugin API surface
- distributed event transport
- external queue infrastructure
- final approval or permission UX

Those decisions should come later, after the runtime skeleton exists.

## References

This design intentionally aligns with the architectural direction seen in `akashic-agent`, especially:

- pipeline-centered execution
- restrained event bus usage
- narrow `ProcessingState`
- proactive-driven drift entry

Reference materials:

- `https://github.com/kachofugetsu09/akashic-agent`
- `https://raw.githubusercontent.com/kachofugetsu09/akashic-agent/main/README.md`
- `https://raw.githubusercontent.com/kachofugetsu09/akashic-agent/main/_handbook/plugins-tutorial.md`
- `https://raw.githubusercontent.com/kachofugetsu09/akashic-agent/main/_handbook/proactive-guide.md`
- `https://raw.githubusercontent.com/kachofugetsu09/akashic-agent/main/_handbook/drift-guide.md`
