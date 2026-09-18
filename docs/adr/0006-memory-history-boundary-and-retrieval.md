# Memory and History storage boundary

Status: accepted

The personal memory system separates original task evidence from derived memory state. Daily append-only History Markdown contains timestamped user queries only. Separate session event records preserve complete task trajectories, including assistant messages and model-visible tool results; source references resolve to these records. Recent Markdown presents the active task and latest N completed tasks; Pending Markdown presents asynchronous extraction candidates without entering model context. `memory.db` stores Pending and Memory facts, validity windows, provenance references, and BM25/vector indexes; `memory.md` is a generated, structured user-profile prefix and is not a retrieval source. No separate history database is needed initially.

Memory retrieval uses Query Rewrite followed by parallel BM25 and available vector search, RRF rank fusion, validity filtering, and deduplication. HyDE requires usable vectors and a successful first vector pass, and runs only when that pass yields fewer than three valid facts or its best relevance is below the configured threshold. Default retrieval returns current facts; temporal queries may include invalidated history. Dynamic results are limited to eight facts and 1,000 tokens.

The model-facing surface is limited to `memory_search` and `memory_manage` (`remember`, `correct`, and `forget`). Task recording, extraction, conflict classification, indexing, consolidation, HyDE, and `memory.md` regeneration remain internal runtime or worker operations. This keeps timestamps, provenance, and conflict invariants outside model-generated SQL or file edits and follows the high-level tool surfaces used by Mem0 and LangMem while preserving the temporal source/fact separation found in Zep/Graphiti.

## Consequences

- History remains readable and independently auditable when Memory is deleted or rebuilt.
- Memory recall never treats the profile Markdown as the database of record.
- The first implementation does not need a second history database or algorithm-specific tools.
- Search indexes can be rebuilt from persisted facts. Re-extraction from task trajectories may produce different facts; corrections, forgetting decisions, and profile versions must be backed up with the database rather than assumed recoverable from user queries alone.

The explicit component and transaction ownership introduced by #38 is recorded in [ADR 0011](0011-memory-components-and-transactions.md), including atomic fact/profile intents and post-commit file recovery.

#39 makes keyword-only operation explicit. Facts and FTS commit atomically; vector generation runs in an application-owned worker, outside database transactions and foreground search. Persisted content/model/dimension signatures identify missing work across restarts without a separate queue; failed calls preserve previously stored vectors, while recall accepts only signatures matching current facts and configuration. Each call captures a configuration generation, and publication rejects stopped workers, changed configurations, changed text and forgotten facts. Missing configuration leaves existing vector storage untouched; a configured dimension change rebuilds the incompatible derived vector table. The worker wakes on indexed fact changes and configuration changes, and polls every 60 seconds for retries. This retains the single-process architecture and existing storage format; foreground query embedding still has provider latency, but no library embedding work.
