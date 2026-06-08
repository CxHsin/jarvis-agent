# Jarvis Tooling Framework Design

## Overview

This spec defines the next expansion direction for `jarvis-agent` after the runnable MVP.

The focus of this phase is the built-in tool system.

The goal is to evolve Jarvis from:

- a Telegram agent with a few directly wired tools

into:

- a small but well-structured tool platform that can grow safely

This design intentionally borrows the core ideas from `kachofugetsu09/akashic-agent` without copying its full plugin and MCP complexity.

## Product Goal

This phase is successful when Jarvis can:

- register built-in tools through a central registry
- expose only a small always-on tool set to the model by default
- discover deferred tools through a meta tool such as `tool_search`
- enforce one unified execution path for every tool call
- attach risk and policy metadata to tools
- make future memory tools fit naturally into the same runtime

## Why This Direction

The current Jarvis tool layer is enough for a runnable MVP, but it will break down as soon as the tool count grows.

Today the system has these limits:

- tool definitions and execution policy are merged into one thin runtime
- every tool is always visible to the model
- tools have no shared metadata beyond name, description, and schema
- there is no deferred exposure mechanism
- there is no stable place for risk policy, timeout policy, or execution governance

The key lesson borrowed from `akashic-agent` is that tools should become a subsystem, not just a list of classes.

## Scope

### In Scope

- redesign of the built-in tool framework
- central tool registry
- central tool executor
- tool metadata model
- always-on vs deferred tool visibility
- lightweight `tool_search` meta tool
- unified tool execution result model
- logging and policy attachment points
- migration of current built-in tools into the new framework

### Out of Scope

- full plugin marketplace
- MCP-first architecture
- workflow engine
- long-term memory implementation
- approval UI
- multi-user permissions model

## Recommended Approach

Three directions were considered:

1. keep the current runtime and only add metadata
2. build a lightweight internal tool platform with deferred discovery
3. copy a larger plugin-oriented architecture from `akashic-agent`

The recommended direction is `2`.

Reasoning:

- it solves the real next problem, which is tool growth and tool selection
- it stays small enough for the current Jarvis codebase
- it preserves a path toward memory tools and plugins later
- it avoids overbuilding before the project has enough tool surface area

## Architecture

The new tool system should be split into five modules inside `jarvis/tools/`.

### `base.py`

Responsibilities:

- define the core `Tool` contract
- define tool metadata shape
- define standard execution request and result types
- define shared schema validation entry points

Each tool should define:

- `name`
- `description`
- `parameters`
- `risk_level`
- `default_timeout_seconds`
- `execute(...)`

The contract should support future optional flags such as:

- `requires_confirmation`
- `source_type`
- `search_hint`

### `registry.py`

Responsibilities:

- register and unregister tools
- hold tool metadata separately from execution
- return tool schemas for model exposure
- separate `always_on` tools from `deferred` tools
- support exact lookup and lightweight search lookup

This is the main structure borrowed from `akashic-agent`.

Jarvis should keep it smaller, but the boundary should be the same: registry manages tool inventory, not execution.

### `executor.py`

Responsibilities:

- execute one tool call through a single code path
- validate parameters
- enforce timeouts
- apply visibility checks
- wrap errors into standard results
- log each call in a consistent format

This layer should not know Telegram details.

It should only know:

- which tool is being called
- what arguments were requested
- what the execution policy says
- what result was produced

### `runtime.py`

Responsibilities:

- coordinate registry and executor
- provide visible tool definitions for the current turn
- manage unlocked deferred tools for the current turn
- expose a small API to the agent loop

The runtime becomes the bridge between the agent and the tool subsystem.

It should replace the current pattern where `ToolRuntime` both stores tools and executes them directly.

### `catalog.py`

Responsibilities:

- build and register built-in tools in one place
- keep wiring out of `app/main.py`
- make future tool bundles easy to add

The target outcome is that runtime setup becomes closer to:

- build registry
- register built-in tools
- build executor
- build runtime

instead of manually instantiating and passing a raw list.

## Tool Visibility Model

This is the most important behavioral change in the design.

Tools should be split into two categories.

### Always-On Tools

These are exposed to the model at the start of every turn.

Initial recommendation:

- `tool_search`
- `read_file`
- `web_search`
- `fetch_url`

These tools are either foundational or low-risk enough to stay visible by default.

### Deferred Tools

These are registered in the system but not shown to the model initially.

Initial recommendation:

- `write_file`
- `shell_exec`
- future state-mutating or high-risk tools

Deferred tools must not execute successfully unless they have been unlocked during the current turn.

## Deferred Tool Discovery

Jarvis should add a lightweight meta tool named `tool_search`.

Its purpose is not to execute work directly.

Its purpose is to help the model discover which deferred tool should be loaded into the current turn.

The first version should support:

- keyword matching over `name`, `description`, and optional `search_hint`
- exact selection syntax such as `select:write_file`
- returning a small ranked list of matching tools
- filtering by risk level if needed later

The result should include:

- tool name
- short summary
- risk level
- whether the tool is always-on or deferred

## Turn-Level Visibility Flow

The tool flow for one user turn should be:

1. agent starts with only always-on tool schemas
2. model answers directly or calls an always-on tool
3. if the model needs a hidden capability, it calls `tool_search`
4. runtime returns matching deferred tools
5. selected tool names are unlocked for the current turn
6. agent re-queries the model with the expanded visible tool set
7. model calls the unlocked tool
8. turn ends and unlocked state is cleared

This keeps the prompt smaller and lowers the chance of accidental dangerous calls.

## Guardrail for Hidden Tools

If the model directly calls a deferred tool that has not been unlocked, execution should fail in a controlled way.

The runtime should return a plain error telling the model to use `tool_search` first.

This guard is important because otherwise deferred tools are only hidden cosmetically and the model may still hallucinate or guess them.

## Tool Execution Model

Every tool call should be converted into a standard request object and produce a standard result object.

### `ToolCallRequest`

Minimum fields:

- `call_id`
- `tool_name`
- `arguments`
- `chat_id`
- `channel`
- `visible_tools`
- `unlocked_tools`

### `ToolCallResult`

Minimum fields:

- `tool_name`
- `status`
- `content`
- `structured`
- `duration_ms`
- `risk_level`

Recommended `status` values:

- `success`
- `error`
- `denied`
- `timeout`
- `unknown`

## Risk Model

Jarvis should assign each tool one of three risk levels.

### `read-only`

Examples:

- `read_file`
- `web_search`
- `fetch_url`
- future `recall_memory`

### `write`

Examples:

- `write_file`
- future `memorize`
- future local state update tools

### `external-side-effect`

Examples:

- `shell_exec`
- future message send tools
- future external write APIs

The first version does not need complex approval handling.

It does need policy attachment points so future control can be added without rewriting the tool layer.

## Execution Governance

The executor should enforce these checks in order:

1. tool exists
2. tool is currently visible or unlocked
3. arguments satisfy schema
4. execution stays within timeout
5. result is normalized into one standard shape
6. call is logged with duration and status

This is where Jarvis should centralize behavior that is currently scattered or absent.

## Result Normalization

Tool outputs should no longer vary arbitrarily between plain text, JSON strings, and exceptions.

Each tool should return a normalized result with these logical parts:

- `content`: short text for the model to read
- `structured`: optional machine-readable payload
- `artifact_refs`: optional future references to files, screenshots, or long outputs

The first implementation may still serialize the payload to text when sending it back to the model, but the internal result type should already preserve structure.

## Mapping Current Tools

The current built-in tools should migrate into the new framework like this:

- `read_file`: always-on, `read-only`
- `web_search`: always-on, `read-only`
- `fetch_url`: always-on, `read-only`
- `write_file`: deferred, `write`
- `shell_exec`: deferred, `external-side-effect`

This split keeps the default model surface useful while moving side-effect tools behind discovery.

## Agent Integration

The current integration path is:

`app/main.py -> ToolRuntime -> Agent`

The new path should remain similarly small from the outside:

`app/main.py -> tool catalog -> registry + executor + runtime -> Agent`

The agent loop should not know how tools are registered internally.

The agent only needs:

- current visible tool definitions
- execution entry point for a tool call
- possible updated visible tool set after `tool_search`

## Testing

This phase should add tests for the tool framework, not just individual tools.

Required test areas:

1. registry registers and lists tools correctly
2. always-on tools appear in visible schemas by default
3. deferred tools do not appear until unlocked
4. direct execution of hidden tools is rejected
5. `tool_search` returns expected matches
6. executor wraps timeout and error cases consistently
7. runtime clears unlocked tool state between turns
8. agent integration still completes a normal tool round trip

## Migration Plan

The implementation should proceed in this order.

### Phase 1

- add new core tool data types
- add registry
- add executor
- refactor runtime to use both

### Phase 2

- migrate existing tools to the new base contract
- add risk metadata and default timeouts
- centralize built-in tool wiring in `catalog.py`

### Phase 3

- add `tool_search`
- implement always-on and deferred visibility
- enforce hidden-tool rejection

### Phase 4

- update the agent loop to support turn-level unlock flow
- expand tests around selection and runtime behavior

## Acceptance Criteria

This tooling framework phase is complete when all of the following are true:

1. built-in tools are registered through a central registry
2. the agent starts each turn with only always-on tools visible
3. deferred tools can be discovered through `tool_search`
4. hidden deferred tools cannot be executed directly
5. all tool calls pass through a central executor with timeout, logging, and normalized result handling
6. current built-in tools still function after migration
7. the design leaves a clean path for future memory tools to join the same runtime

## Evolution Path After This Phase

Once this framework is stable, the next logical steps are:

- add memory tools such as `recall_memory` and `memorize`
- add policy hooks for higher-risk tools
- add optional tool bundles or plugin loading
- add stronger tool ranking or search quality
- add workflow composition after the single-tool model is stable

## Non-Goals for This Spec

This spec does not define:

- the final memory architecture
- plugin packaging format
- MCP server lifecycle
- approval UX
- multi-turn workflow planner

Those should come only after the internal tool framework is stable.
