# Personal Agent Phase 4 Design: Memory Read/Write Policy

## Goal

Define the Phase 4 memory policy for the current local agent after Phase 3 introduced file-based long-term memory.

This phase does not add new storage substrates. It defines when memory and short-term context should be injected, updated, or ignored during a turn.

The design is driven by a concrete failure mode already observed in the current system:

- when the user asks an identity question such as "who are you", the model may fail to consistently follow `system_prompt`
- the current agent stores successful assistant replies inside per-chat short-term history
- later requests replay that assistant history back into the model
- a wrong assistant self-description can therefore be amplified by short-term context replay

This means the root problem is not only prompt strength. The root problem is:

- initial identity drift
- followed by assistant-history replay that reinforces the drift

There is also a separate runtime issue:

- Telegram polling `offset` is currently process-local only
- after restart, old updates may be consumed again
- this can replay old incorrect dialogue chains and make it appear that long-term memory caused the issue

That transport-layer issue is documented here because it affects observability, but it is not itself the Phase 4 memory policy problem.

## Why Phase 4 Exists

Phase 3 introduced durable memory files:

- `MEMORY.md`
- `RECENT_CONTEXT.md`
- `PENDING.md`
- `HISTORY.md`

Once these files exist, the next systemic problem is no longer storage. The next problem is policy:

- which context should participate in the next turn
- which information should be persisted
- which sources are trusted enough to influence future behavior
- which sources should be ignored or downgraded

Without an explicit policy, the system risks treating all prior context as equally valid:

- user messages
- assistant replies
- stable facts
- recent working context
- event logs

Those categories are not equally trustworthy and should not be injected or persisted using the same rules.

## Scope

Phase 4 introduces one new boundary:

- `MemoryPolicy` or equivalent policy layer

Responsibilities of this boundary:

- decide which durable memory artifacts become prompt context
- decide which short-term history elements should be replayed
- decide which information can become long-term memory candidates
- separate trusted facts from low-trust or transient information
- preserve explainable, testable policy decisions

This phase does not redesign the Phase 3 storage boundary itself.

## Non-Goals

Explicitly out of scope for this phase:

- vector retrieval
- semantic search
- background consolidation jobs
- optimizer loops
- automated memory summarization pipelines
- plugin-based memory contributors
- changing long-term memory away from readable markdown files
- fixing Telegram polling architecture in the same phase

## Current Failure Analysis

### Failure 1: Assistant Identity Drift Amplified by History Replay

Current behavior:

- the agent always injects `system_prompt`
- the agent also replays prior user and assistant turns for the same chat
- successful assistant turns are appended into short-term history
- later turns replay those assistant responses verbatim

If the model answers one identity question incorrectly, the system can then feed that wrong identity claim back into subsequent turns as assistant history.

This creates a reinforcing loop:

1. model drifts once
2. drift is stored as assistant history
3. drift is replayed into later prompts
4. replay increases the chance of repeated drift

This is a context policy problem, not only a prompt wording problem.

### Failure 2: Telegram Update Replay After Restart

Current behavior:

- Telegram `offset` lives only in process memory
- a restart resets the polling position
- old updates that were not skipped at Telegram level can be consumed again

This can replay old message chains and make the system appear to "remember" prior incorrect identity behavior.

This issue is real and should be fixed, but it is classified as a runtime or adapter correctness issue rather than a long-term memory policy issue.

## Design Summary

Phase 4 adopts a middle-path policy:

- do not fully trust assistant history
- do not remove all assistant continuity
- separate context injection rules from long-term write rules
- prefer user-side information as the source of durable memory

This is intentionally lighter than the full `akashic-agent` architecture, but it adopts the same core idea:

- identity, stable facts, recent working context, event logs, and candidate memory are different classes of information
- they require different trust levels and different handling rules

## Policy Model

The system should reason about context in layers, not as one undifferentiated prompt blob.

### 1. Fixed Identity Layer

Purpose:

- define who the agent is
- provide stable self-model and response contract

Source:

- `system_prompt`

Policy:

- this layer has highest priority for identity questions
- assistant self-descriptions from prior turns must not override it
- assistant-generated identity claims must not be promoted into long-term memory

Consequence:

- "who are you" should be answered from fixed identity policy, not from replayed assistant text

### 2. Long-Term Memory Layer

Purpose:

- provide durable, reusable user-related information

Sources:

- `MEMORY.md`
- selected portions of `RECENT_CONTEXT.md`
- optionally selected portions of `PENDING.md`

Policy:

- only policy-approved content is injected
- files are not treated as equal-trust raw prompt text
- `HISTORY.md` is not a default prompt input

### 3. Short-Term Conversation Layer

Purpose:

- preserve local turn continuity within the chat

Sources:

- prior user turns
- selected assistant continuity signals

Policy:

- user history remains the default short-term source
- assistant history is no longer replayed as a fully trusted verbatim stream
- assistant history must be filtered before it can influence later turns

## Context Injection Policy

### Core Rule

The agent should stop treating the same-chat prompt context as:

- full prior user history
- full prior assistant history
- full long-term memory file dump

Instead, prompt context should be assembled by trust-aware policy.

### User History

Default behavior:

- keep replaying recent user turns for same-chat continuity

Why:

- user messages are the primary source of intent, constraints, corrections, preferences, and follow-up references

### Assistant History

Default behavior:

- do not replay assistant history as a fully trusted default transcript

Assistant content that should be excluded by default:

- identity statements
- self-descriptions
- factual claims about the world that were generated by the model
- inferred summaries that were never confirmed by the user
- speculative statements about user intent or user state

Assistant content that may be retained in limited form:

- task continuity markers
- immediate structural commitments relevant to the next turn
- local response-format continuity when directly referenced by the user

Examples:

- acceptable continuity: "I will compare option A and B next"
- acceptable continuity: "I listed three items; user now says continue with item 2"
- not acceptable as trusted replay: "I am DeepSeek"
- not acceptable as trusted replay: "the user probably prefers X" unless the user confirmed it

### Identity Questions

Identity-sensitive questions should be treated as a special policy case.

Examples:

- "who are you"
- "what model are you"
- "are you the same assistant as last time"

Policy:

- answer from fixed identity layer first
- do not use prior assistant identity text as supporting evidence
- do not allow assistant history replay to redefine identity

This special handling is justified because identity drift has asymmetric damage:

- a single wrong self-description can contaminate later turns
- replayed contamination is difficult to distinguish from intentional design

### Long-Term Memory Injection Defaults

Default prompt injection behavior:

- inject `MEMORY.md`
- inject relevant or selected `RECENT_CONTEXT.md`
- do not inject `HISTORY.md` by default
- only inject `PENDING.md` conditionally, if there is a concrete policy reason

The key point is that not every readable memory artifact is also a default prompt artifact.

## Long-Term Memory Write Policy

Prompt participation and long-term persistence must be governed by separate rules.

Being present in context does not imply:

- it is true
- it is stable
- it should be remembered long-term

### Default Source of Long-Term Memory

Preferred source:

- user messages

Rationale:

- user-side statements are the most direct source for goals, identity, preferences, constraints, and corrections
- assistant outputs are generated text and should not be treated as durable facts by default

### Trust Tiers

#### High Trust

Eligible for `MEMORY.md`:

- user-explicit stable facts
- user-explicit long-term preferences
- user-explicit long-term goals
- user-explicit durable constraints

#### Medium Trust

Eligible for `PENDING.md` first:

- potentially useful user information expressed only once
- user information that may matter later but is not yet clearly stable
- multi-turn patterns that look meaningful but still need confirmation

#### Low Trust

Not eligible for direct long-term storage:

- assistant summaries not confirmed by the user
- assistant guesses
- assistant self-description
- assistant identity claims
- assistant inferences about user psychology or preference

### Write Rules

- `MEMORY.md` accepts only high-trust, sufficiently stable information
- `PENDING.md` is the default buffer for maybe-useful but not-yet-stable information
- assistant output must not directly write into `MEMORY.md`
- assistant output may only influence `PENDING.md` if future policy explicitly allows low-trust staging
- assistant identity or self-model content must not be written into durable memory artifacts

This prevents a second class of failure:

- not only can wrong assistant text affect the next turn
- wrong assistant text can otherwise become durable memory and become harder to unwind

## Memory File Responsibilities

The four Phase 3 files should no longer be treated as interchangeable general-purpose note buckets.

Each file must have a distinct role and source policy.

### `MEMORY.md`

Purpose:

- stable long-term fact store

Accepts:

- relatively durable user facts
- durable user preferences
- durable user goals
- durable user constraints

Rejects by default:

- assistant self-description
- assistant guesses
- one-off transient user context
- event-log detail

Injection policy:

- default prompt input

### `RECENT_CONTEXT.md`

Purpose:

- recent working context

Accepts:

- current topic state
- active short-term goals
- recently changed preferences that may still matter
- session-adjacent context derived primarily from user-side information

Allowed assistant influence:

- only indirect task continuity signals
- not durable facts
- not identity

Injection policy:

- default prompt input, potentially selected or summarized rather than blindly full-file

### `PENDING.md`

Purpose:

- candidate memory buffer

Accepts:

- useful but not-yet-stable user information
- tentative memory candidates
- items worth later confirmation or consolidation

Rejects by default:

- direct promotion into stable memory without further policy

Injection policy:

- conditional prompt input only
- not guaranteed default injection

### `HISTORY.md`

Purpose:

- event ledger

Accepts:

- important state changes
- significant milestones
- notable events worth inspection or later consolidation

Injection policy:

- not a default prompt input
- primarily for auditability, inspection, grep, or future consolidation workflows

This file exists to preserve "what happened", not to act as a fully trusted next-turn guidance source.

## Decision Matrix

| Information type | Short-term replay | `RECENT_CONTEXT.md` | `PENDING.md` | `MEMORY.md` |
| --- | --- | --- | --- | --- |
| User explicit stable fact | Yes | Optional | Optional | Yes |
| User short-term task or topic | Yes | Yes | Optional | No |
| User one-off but maybe useful fact | Yes | Optional | Yes | No |
| Assistant identity claim | No | No | No | No |
| Assistant task continuity statement | Filtered | Optional | No | No |
| Assistant unconfirmed summary | No | No | Optional at most | No |
| Important event or milestone | Optional | Optional | No | No, prefer `HISTORY.md` |

## Observability Requirements

Phase 4 is not complete unless policy decisions are explainable.

The runtime should make it possible to inspect or log:

- whether assistant history was replayed for a turn
- whether a filtered assistant segment was excluded
- which memory files participated in prompt assembly
- whether a candidate was considered stable memory or pending memory

The goal is not verbose tracing everywhere. The goal is to make identity drift and memory contamination diagnosable.

## Runtime Boundary Note: Telegram Offset Persistence

The Telegram polling `offset` persistence problem should be tracked explicitly alongside Phase 4, but not absorbed into the Phase 4 abstraction.

Classification:

- runtime or adapter correctness issue

Why it matters for Phase 4:

- old updates can be replayed after restart
- replay can reintroduce old incorrect short-term context
- this can be misdiagnosed as long-term memory corruption

Required note for future evaluation:

- any test or observation about memory-policy behavior must account for possible transport replay until offset persistence is fixed

What this design does not do:

- it does not redefine Telegram transport ownership
- it does not make `MemoryPolicy` responsible for update consumption state

## Architecture Impact

### New Boundary

Introduce a policy-focused unit such as:

- `MemoryPolicy`
- `ContextPolicy`
- or a similarly narrow decision component

Responsibilities:

- classify context sources by trust and role
- decide prompt injection set
- decide replay eligibility for short-term history
- decide durable write target category

### Unchanged Boundaries

- `MemoryStore` remains storage-oriented
- `ConversationStore` remains short-term state storage
- `TelegramBot` remains channel adapter
- `LLMClient` remains model transport logic

### Future Replaceability

This phase should preserve future evolution paths without implementing them now:

- future `SELF.md` or explicit self-model storage
- future summarization or consolidation
- future semantic retrieval
- future differentiated write pipelines

Those are compatible with this policy design, but not required for Phase 4 completion.

## Testing Implications

Deterministic policy behavior introduced in this phase should have automated coverage.

Key cases:

- assistant identity drift in one turn does not redefine later identity answers
- user history remains available for follow-up continuity
- assistant continuity information can be retained in limited approved cases
- assistant self-description is excluded from replay
- user-stable facts are eligible for long-term persistence
- low-trust assistant text is rejected from direct `MEMORY.md` writes
- `HISTORY.md` is not injected by default
- policy behavior remains explainable under restart-replay contamination scenarios

## Failure Semantics

If policy classification fails:

- the system should prefer a conservative fallback
- conservative fallback means excluding low-trust assistant-derived context rather than promoting it

If memory selection fails:

- the system should degrade toward minimal safe context, not maximal raw replay

If runtime replay occurs because of Telegram offset reset:

- the system may still see repeated user inputs
- this must not be confused with successful long-term memory retrieval

## Definition of Done

Phase 4 is done when all of the following are true:

- context injection rules distinguish user history from assistant history
- identity-sensitive handling is explicit and testable
- long-term write rules distinguish stable memory from pending memory
- each memory file has a clear responsibility and source boundary
- `HISTORY.md` is no longer treated as a default next-turn prompt artifact
- logs or inspection can explain policy decisions
- tests cover the deterministic policy cases introduced by this phase

## Decision Summary

This design chooses:

- filtered assistant continuity instead of full assistant replay
- user-first long-term memory extraction
- separate rules for prompt injection and durable writes
- explicit identity-sensitive policy handling
- differentiated responsibilities for `MEMORY.md`, `RECENT_CONTEXT.md`, `PENDING.md`, and `HISTORY.md`
- explicit recognition that Telegram offset persistence is a separate correctness issue that can pollute Phase 4 observations

## Out of Scope Follow-Ups

Follow-up work that may happen after Phase 4, but is not required to complete it:

- persistent Telegram polling offset
- explicit self-model file such as `SELF.md`
- memory consolidation from `PENDING.md` into `MEMORY.md`
- event-log summarization
- semantic retrieval for large memory sets
- richer inspection surfaces for policy traces
