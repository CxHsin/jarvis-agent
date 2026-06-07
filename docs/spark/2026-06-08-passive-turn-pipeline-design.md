# Passive Turn Pipeline Design

## Goal

Define a forward-looking but still single-process architecture evolution for the passive turn path of this project, using `kachofugetsu09/akashic-agent` as a structural reference rather than a feature checklist.

The immediate target is not feature parity with `akashic-agent`. The target is to remove the current centralization of passive-turn logic inside `AgentService.generate_reply()` and replace it with a clear turn pipeline that can absorb future complexity without collapsing back into a single orchestration function.

## Scope

This design covers:

- passive turn architecture evolution
- module boundary changes around `AgentService`
- prompt assembly, reasoning, post-reply, and commit decomposition
- a staged refactor path that keeps the project single-process and operational throughout

This design does not cover:

- proactive feature redesign
- drift runner redesign
- plugin phase runtime redesign
- vector memory or advanced retrieval
- dashboard or inspection UI work

## Current Project Assessment

The current repository has already established the core subsystems expected for an early personal-agent runtime:

- transport entry through Telegram
- short-term per-chat conversation state
- file-based long-term memory
- memory write policy
- tool loop
- plugin host
- proactive runtime
- drift runtime

However, the passive reply path remains concentrated in `app/agent.py` inside `AgentService.generate_reply()`.

That function currently owns all of the following:

- input normalization and validation
- memory snapshot loading
- self-model repair and normalization
- conversation history loading
- plugin context construction
- prompt construction
- before-model plugin hook execution
- LLM/tool-loop execution
- after-model plugin hook execution
- conversation append
- memory write plan generation
- memory writes
- consolidation
- after-turn plugin hook execution

This means the project already has subsystem boundaries, but the passive turn itself is not yet a first-class runtime boundary.

## Reference Project Analysis: `akashic-agent`

`akashic-agent` is significantly larger, but the most relevant lesson is architectural rather than functional.

### What Matters Most In The Reference

The strongest design choice in `akashic-agent` is that a turn is treated as an explicit runtime pipeline rather than hidden inside a transport-facing service.

Useful characteristics from the reference:

- passive turn orchestration is separated from transport bootstrap
- turn execution is broken into named lifecycle stages
- prompt construction, reasoning, and commit concerns are not all owned by a single service method
- plugin behavior attaches to lifecycle boundaries instead of forcing more branches into the core service

### What Should Not Be Copied Yet

Several parts of `akashic-agent` are too mature for the current project stage:

- full lifecycle phase runtime
- slot-based plugin data bus
- complex MCP-oriented proactive stack
- broad multi-channel and dashboard integration surfaces

Those designs solve later-stage complexity. Adopting them now would likely over-abstract the current repository before the passive turn boundary itself is stabilized.

## Core Design Decision

Adopt a `Turn Pipeline` architecture for passive turns.

This is intentionally between two extremes:

- more ambitious than a shallow file split
- less ambitious than directly cloning `akashic-agent`'s full lifecycle runtime

The passive turn becomes an explicit orchestrated pipeline with fixed stages and a shared turn context object.

## Recommended Architecture

### Top-Level Shape

The passive-turn stack should evolve toward:

- `AgentService`
  - public facade
  - validates inputs
  - acquires per-chat lock
  - invokes passive turn orchestration
  - returns final reply text
- `PassiveTurnOrchestrator`
  - owns stage ordering
  - does not own detailed memory/tool/plugin logic
- `TurnContext`
  - mutable state container for one passive turn
- `TurnStage` implementations
  - each stage owns one slice of the turn

### Required Turn Stages

The initial pipeline should contain five stages.

#### 1. `LoadTurnContextStage`

Responsibilities:

- normalize user input
- load memory snapshot
- ensure normalized self-model state
- load short-term conversation history
- determine available tools
- build the base plugin turn context

Non-responsibilities:

- prompt assembly
- model invocation
- persistence

#### 2. `BuildPromptStage`

Responsibilities:

- assemble system prompt
- inject memory sections
- inject plugin-provided context sections
- replay conversation history
- append current user input

Non-responsibilities:

- tool execution
- memory write planning

#### 3. `RunReasoningStage`

Responsibilities:

- execute `before_model_call` plugin hooks
- choose direct LLM reply or tool loop path
- capture raw reply text

Non-responsibilities:

- memory writes
- conversation append
- consolidation

#### 4. `PostReplyStage`

Responsibilities:

- execute `after_model_call` plugin hooks
- collect turn notes
- perform reply-adjacent postprocessing before commit

Non-responsibilities:

- durable writes

#### 5. `CommitTurnStage`

Responsibilities:

- append short-term conversation history
- build memory write plan
- persist memory updates
- run consolidation
- execute `after_turn` plugin hooks
- emit final turn commit result

Non-responsibilities:

- prompt assembly
- LLM execution

## Data Model Changes

### `TurnContext`

Introduce a turn-scoped state object to hold all important passive-turn state explicitly instead of leaving it scattered through local variables.

Suggested contents:

- `chat_id`
- `user_text`
- `normalized_user_text`
- `history`
- `memory_snapshot`
- `available_tools`
- `extra_context_sections`
- `prompt_messages`
- `reply_text`
- `turn_notes`
- `memory_candidates`
- `memory_write_plan`
- `plugin_outcomes`

The object may be mutable in this stage of the project. Immutability is not required yet. The important step is making the turn state explicit.

### `TurnCommitResult`

Introduce a final result object for passive turns.

Suggested contents:

- `reply_text`
- `memory_write_plan`
- `turn_notes`
- `plugin_outcomes`

This makes the passive turn produce a structured outcome rather than only a raw reply string.

## Module Refactor Recommendations

### New Modules

Recommended new structure:

- `app/turns/context.py`
- `app/turns/orchestrator.py`
- `app/turns/stages/load_context.py`
- `app/turns/stages/build_prompt.py`
- `app/turns/stages/run_reasoning.py`
- `app/turns/stages/post_reply.py`
- `app/turns/stages/commit.py`

### `AgentService`

Reduce `AgentService` to a facade over passive turn orchestration.

After refactor, `AgentService.generate_reply()` should only:

- validate non-empty input
- acquire the conversation lock
- call the passive orchestrator
- return `reply_text`

It should no longer directly own turn internals.

### `MemoryPolicy`

The current `MemoryPolicy` is carrying two responsibilities:

- prompt-facing memory section construction
- memory write decision logic

That is acceptable in early phases, but becomes awkward once passive turns are stage-based.

Recommended adjustment:

- keep `MemoryPolicy` as the owner of memory injection rules and memory write planning
- move full prompt construction ownership into a higher-level `PromptAssembler`

Concretely:

- `MemoryPolicy` should provide memory sections and write plans
- `PromptAssembler` should decide final prompt message ordering and composition

This narrows the memory module back toward domain policy instead of making it a passive-turn controller.

### `PluginHost`

Do not redesign `PluginHost` into a full lifecycle runtime yet.

Keep it as a hook aggregator, but change the passive-turn code so hooks are invoked from stage boundaries rather than from the middle of a monolithic service method.

This preserves compatibility while improving placement clarity.

### `ToolLoop`

Keep `ToolLoop` inside the reasoning subsystem.

It should remain a dependency of `RunReasoningStage`, not of the top-level orchestrator. That keeps tool execution as one implementation strategy of reasoning rather than a cross-cutting control structure.

## Failure Semantics

The passive-turn pipeline should make failure behavior explicit.

### Reply Generation Failures

If prompt construction or model execution fails before a reply is produced:

- the turn fails
- no conversation append occurs
- no memory write occurs
- no consolidation occurs

### Commit Failures After Reply Exists

If a reply is successfully produced but commit work fails:

- the reply should still be returned to the user
- memory persistence failures should be logged
- consolidation failures should be logged
- plugin `after_turn` failures should not invalidate the reply

This preserves current pragmatic behavior while making it explicit.

### Why This Matters

Without explicit failure boundaries, future refactors around proactive/drift reuse will inherit unclear guarantees about what counts as a committed turn.

## Testing Strategy

The refactor should improve test shape rather than merely move code around.

### New Test Layers

- stage unit tests
  - each stage can be tested with a prepared `TurnContext`
- orchestrator order tests
  - verifies stage execution order and handoff
- compatibility tests for `AgentService`
  - verifies the public entrypoint still behaves as before

### Existing Tests To Preserve

The current deterministic tests around:

- agent behavior
- memory store
- consolidation
- plugin host
- tool execution

should remain, but their center of gravity should gradually move from full-service tests to stage-level tests where possible.

## Staged Refactor Plan

The refactor should be implemented in four stages.

### Stage 1: Introduce `PassiveTurnOrchestrator`

Objective:

- separate public service entry from turn execution

Actions:

- create `PassiveTurnOrchestrator`
- move the existing `generate_reply()` body into the orchestrator with minimal behavior changes
- keep `AgentService` as a thin facade

Expected outcome:

- no meaningful behavior change
- immediate reduction in centralization pressure

### Stage 2: Introduce `TurnContext` And Split Into Five Stages

Objective:

- make passive turns explicit runtime objects

Actions:

- create `TurnContext`
- split the orchestrator implementation into the five fixed stages
- keep behavior equivalent to the pre-refactor version

Expected outcome:

- passive turn execution becomes architecture-shaped rather than function-shaped

### Stage 3: Narrow `MemoryPolicy`

Objective:

- separate memory policy from prompt assembly ownership

Actions:

- introduce `PromptAssembler`
- move whole-prompt assembly out of `MemoryPolicy.build_messages()`
- retain memory section logic and write-plan logic inside `MemoryPolicy`

Expected outcome:

- cleaner memory boundary
- easier future reuse from proactive or drift-adjacent contexts

### Stage 4: Formalize Commit Semantics

Objective:

- separate reply generation from side-effect commit work

Actions:

- move append/history/memory/consolidation/after-turn steps into `CommitTurnStage`
- produce a structured `TurnCommitResult`
- make error handling explicit

Expected outcome:

- clearer guarantees
- better observability
- better basis for future non-passive turn reuse

## Why This Path Is Better Than The Alternatives

### Not Just A Shallow Split

A light split of `AgentService` into helper methods would reduce file size but would not establish a real passive-turn boundary. The next wave of complexity would still collect in one place.

### Not Full `akashic-agent` Phase Runtime Yet

A direct jump to lifecycle phases, slot exports, and plugin topology would be too large relative to the current repository. The current problem is not missing a sophisticated plugin runtime. The current problem is that the passive turn is not yet modeled as its own runtime object.

The turn pipeline solves the actual next-order problem.

## Future Compatibility

This design intentionally preserves future evolution paths.

Once the passive turn is pipeline-based, later work can safely evolve toward:

- finer-grained phase subdivision
- stronger plugin attachment points
- read-side inspection surfaces
- partial reuse of prompt/reasoning/commit stages by proactive or drift paths

Those future steps should remain optional. The current refactor should not force them now.

## Decision Summary

This design chooses:

- passive-turn pipeline over monolithic service orchestration
- forward-looking boundaries over premature frameworkization
- single-process implementation over distributed speculation
- explicit turn context over local-variable orchestration
- stage-based reasoning/commit split over helper-method sprawl

## Definition Of Done

This passive-turn architecture refactor should be considered complete only when all of the following are true.

### Architecture Done

- `AgentService` is a facade, not the passive-turn implementation center
- passive turn execution exists as an explicit orchestrator plus stages
- prompt assembly, reasoning, post-reply, and commit are separate concerns

### Behavior Done

- user-visible reply behavior remains equivalent for existing covered flows

### Failure Semantics Done

- reply-generation failure versus commit failure is explicit in design and code

### Tests Done

- stage-level tests exist for introduced deterministic behavior
- public entry compatibility remains covered

### Scope Done

- the refactor improves passive-turn structure without redesigning unrelated subsystems
