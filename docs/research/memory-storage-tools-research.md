# Memory Storage and Tool Surface Research

Date: 2026-09-16

This note answers two design questions: whether task/history data needs a
separate database from the memory database, and how many memory operations
should be exposed to an agent.

## Findings from mature systems

### Zep / Graphiti

Graphiti describes three logical components: entities, temporal facts, and
episodes. Episodes are the raw data that produced derived entities and facts;
every derived fact can be traced back to an episode. Facts have validity
windows, and when a fact changes the old fact is invalidated rather than
deleted. Graphiti also says it performs incremental ingestion and hybrid
semantic, keyword, and graph retrieval.

Source: [Graphiti README](https://github.com/getzep/graphiti/blob/main/README.md),
sections “What is a Context Graph?” and “Why Graphiti?”.

The same README distinguishes Graphiti from Zep: Zep provides built-in users,
threads, and message storage, while Graphiti is an open-source framework where
the surrounding user/conversation management is built by the integrator. This
is a logical separation of raw conversation/episode data from derived context,
but the documentation does not require a separate SQLite file. Zep's managed
service uses its own context graph engine; Graphiti uses a graph backend and
full-text backend selected by the deployment.

Implication: “history” must be an immutable source/provenance layer, and
“memory” must be a derived, versioned layer. Separate files are an operational
choice, not a requirement imposed by the Zep model. For this local project,
separate `history/` files and `memory.db` give clearer backup, deletion, and
failure boundaries than putting task transcripts in the memory index. A
`history.db` is optional: it is useful only for indexed querying, retention
metadata, or transactional task state. The canonical task trajectory can stay
as daily append-only files, with `memory.db` storing `source_task_id` and
`source_event_id` references.

### Mem0

Mem0 presents a small primary API: add conversation data, search memories, and
manage existing memories. Its quickstart explicitly calls `memory.search(...)`
before generation and `memory.add(...)` after the assistant response. The CLI
also exposes `add` and `search`; the platform/API documentation includes
update/delete operations for memory management.

Source: [Mem0 README](https://github.com/mem0ai/mem0/blob/main/README.md),
sections “Basic Usage” and “CLI”.

Mem0's model is a memory service, not a transcript archive. It accepts messages
as input to extraction, but the agent-facing workflow is still essentially
write (`add`) and read (`search`), with administrative update/delete. This is
evidence against exposing separate tools for BM25, vector search, reranking,
consolidation, or conflict resolution.

### LangMem / LangGraph

LangMem exposes two agent tools in its canonical example:
`create_manage_memory_tool` and `create_search_memory_tool`. The manage tool
lets the agent create/update memory; the search tool retrieves related memory.
The storage implementation is supplied by LangGraph's Store abstraction, with
in-memory and database-backed stores. LangMem also provides a background memory
manager that extracts, consolidates, and updates knowledge without making each
pipeline step an agent-facing tool.

Source: [LangMem README](https://github.com/langchain-ai/langmem/blob/main/README.md),
sections “Key features” and “Creating an Agent”.

Implication: expose only high-level operations to the model. Keep extraction,
deduplication, conflict classification, embedding, BM25, RRF, consolidation,
and archival in trusted application code or scheduled workers.

## Recommendation for Jarvis

### Storage boundary

Use two logical stores with different ownership:

1. **History**: daily append-only task trajectory files. Each line/event keeps
   the original user query and complete task record with `occurred_at` and
   `recorded_at`. This is the audit/source layer and should remain readable
   even if a memory is deleted or rebuilt.
2. **Memory**: `memory.db`, containing candidate and active user facts,
   embeddings/FTS indexes, validity windows, confidence, conflict links, and
   source references back to History.

Do not create `history.db` in the first version. Add one only if requirements
emerge for indexed history search, transactional task metadata, or retention
queries that daily files cannot support. A separate database file for history
would otherwise duplicate the source of truth and introduce synchronization and
deletion semantics without helping memory recall.

The distinction is logical even when implementation later changes: History is
immutable evidence; Memory is rebuildable derived state. `memory.db` recall
must query the memory facts/index, never `memory.md`; `memory.md` is the stable
fixed-prefix profile generated from active facts.

### Agent-facing tool surface

Start with two tools:

- `memory_search(query, include_history=false)`: hybrid retrieval over active
  facts, returning fact text, validity, confidence, and source references.
- `memory_manage(operation, fact or memory_id)`: explicit remember, correct,
  and forget operations. User-initiated edits should be marked as the highest
  confidence source; “forget” ends validity by default and preserves audit
  metadata unless a separate hard-delete command is used.

The runtime itself should call an internal `record_task_event` on every turn;
it does not need to be an LLM tool. Pending extraction, consolidation,
conflict handling, vector/FTS indexing, and `memory.md` regeneration should be
internal workers. This keeps the model tool list small and prevents the model
from bypassing timestamp, provenance, or conflict invariants.

If explicit user commands are later needed, map them to the same two operations
instead of adding tools such as `memory_bm25_search`, `memory_vector_search`,
`memory_consolidate`, or `memory_archive`.

## Sources

- Graphiti, *A Framework for Building Temporal Knowledge Graphs*:
  https://github.com/getzep/graphiti/blob/main/README.md
- Mem0, *The Memory Layer for Personalized AI*:
  https://github.com/mem0ai/mem0/blob/main/README.md
- LangMem, *Long-term memory for agents*:
  https://github.com/langchain-ai/langmem/blob/main/README.md

