"""Hybrid retrieval over versioned facts; indexes are disposable derived state."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import re

from stable_memory import timestamp


def terms(text):
    return re.findall(r"[\u3400-\u9fff]|[a-z0-9]+|[^\W\d_]+", str(text).casefold())


def searchable(text):
    return " ".join(terms(text))


def current(row, now):
    return row["status"] != "forgotten" and row["valid_from"] <= now and (
        row["valid_to"] is None or row["valid_to"] > now)


class MemoryRetrieval:
    def _init_retrieval(self, db):
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(fact_id UNINDEXED, text, subject, predicate, object)")
        db.execute("CREATE TABLE IF NOT EXISTS memory_embedding_cache (fact_id TEXT PRIMARY KEY, signature TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS memory_index_config (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if isinstance(self.embedding_dimensions, bool) or not isinstance(self.embedding_dimensions, int) or not 1 <= self.embedding_dimensions <= 65536:
            raise ValueError("Embedding dimensions must be a positive integer up to 65536")
        try:
            db.execute("SELECT vec_version()")
            previous = db.execute("SELECT value FROM memory_index_config WHERE name='dimensions'").fetchone()
            if previous is None or previous[0] != str(self.embedding_dimensions):
                db.execute("DROP TABLE IF EXISTS memory_vec")
                db.execute("DELETE FROM memory_embedding_cache")
            db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(fact_id TEXT PRIMARY KEY, embedding FLOAT[{self.embedding_dimensions}])")
            db.execute("INSERT OR REPLACE INTO memory_index_config VALUES ('dimensions', ?)", (str(self.embedding_dimensions),))
            self._vec_available = True
        except Exception:
            self._vec_available = False
        # Also migrates old unsegmented FTS rows, without making network calls.
        for row in list(db.execute("SELECT fact_id FROM memory_facts")):
            self._index_fact(db, row["fact_id"])

    def configure_retrieval(self, *, embedding_client=None, rewrite_client=None, embedding_model=None):
        self.embedding_client = embedding_client
        self.rewrite_client = rewrite_client
        self.embedding_model = embedding_model

    def _embed(self, text):
        client = self.embedding_client
        if client is None:
            raise ValueError("Embedding service is not configured")
        result = client.embed(text, model=self.embedding_model) if hasattr(client, "embed") else client(text)
        if isinstance(result, dict):
            result = result.get("embedding") or (result.get("data") or [{}])[0].get("embedding")
        if not isinstance(result, (tuple, list)) or len(result) != self.embedding_dimensions:
            raise ValueError("Embedding dimension mismatch")
        if any(isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) for value in result):
            raise ValueError("Invalid embedding values")
        norm = math.sqrt(sum(value * value for value in result))
        if not norm:
            raise ValueError("Zero embedding")
        return [value / norm for value in result]

    def _index_fact(self, db, fact_id):
        row = db.execute("SELECT fact_id,text,subject,predicate,object FROM memory_facts WHERE fact_id=?", (fact_id,)).fetchone()
        if row:
            db.execute("DELETE FROM memory_fts WHERE fact_id=?", (fact_id,))
            db.execute("INSERT INTO memory_fts VALUES (?,?,?,?,?)",
                       (row["fact_id"], *(searchable(row[key]) for key in ("text", "subject", "predicate", "object"))))

    def _sync_vectors(self):
        """Backfill/retry embeddings outside the fact-writing transaction."""
        if self.embedding_client is None:
            return False
        if not self._vec_available:
            return True
        with self._connect() as db:
            rows = list(db.execute("SELECT fact_id,text FROM memory_facts WHERE status!='forgotten'"))
            cached = dict(db.execute("SELECT fact_id,signature FROM memory_embedding_cache"))
        failed = False
        for row in rows:
            signature = hashlib.sha256(json.dumps(
                [self.embedding_model, self.embedding_dimensions, row["text"]], ensure_ascii=False).encode()).hexdigest()
            if cached.get(row["fact_id"]) == signature:
                continue
            try:
                vector = self._embed(row["text"])
            except Exception:
                vector = None
                failed = True
            with self._lock, self._connect() as db:
                actual = db.execute("SELECT text,status FROM memory_facts WHERE fact_id=?", (row["fact_id"],)).fetchone()
                if not actual or actual["status"] == "forgotten" or actual["text"] != row["text"]:
                    continue
                db.execute("DELETE FROM memory_vec WHERE fact_id=?", (row["fact_id"],))
                db.execute("DELETE FROM memory_embedding_cache WHERE fact_id=?", (row["fact_id"],))
                if vector is not None:
                    db.execute("INSERT INTO memory_vec VALUES (?,?)", (row["fact_id"], json.dumps(vector)))
                    db.execute("INSERT INTO memory_embedding_cache VALUES (?,?)", (row["fact_id"], signature))
        return failed

    def _rewrite(self, query):
        if self.rewrite_client is None:
            return query
        try:
            response = self.rewrite_client.complete([
                {"role": "system", "content": 'Rewrite the user question for personal-memory retrieval. Preserve names, dates and intent. Return JSON {"query":"search terms"} only. Do not answer the question or invent facts.'},
                {"role": "user", "content": query}], [], "none")
            content = response["content"].strip()
            try:
                value = json.loads(content).get("query")
            except (ValueError, AttributeError):
                value = content
            return value if isinstance(value, str) and value.strip() else query
        except Exception:
            return query

    def _lexical(self, query, records):
        query_terms = list(dict.fromkeys(terms(query)))[:64]
        if not query_terms or not records:
            return {}, {}
        expression = " OR ".join('"' + term + '"' for term in query_terms)
        with self._connect() as db:
            rows = db.execute("SELECT fact_id FROM memory_fts WHERE memory_fts MATCH ? ORDER BY bm25(memory_fts), fact_id", (expression,))
            ids = [row["fact_id"] for row in rows if row["fact_id"] in records]
        ranks = {key: index + 1 for index, key in enumerate(ids)}
        quality = {key: len(set(query_terms) & set(terms(records[key]["text"] + " " + records[key]["object"]))) / len(query_terms) for key in ids}
        return ranks, quality

    def _vector(self, query, records):
        if self.embedding_client is None:
            return {}, {}, False
        if not self._vec_available:
            return {}, {}, True
        if not records:
            return {}, {}, False
        try:
            vector = self._embed(query)
            ids = list(records)
            with self._connect() as db:
                # Pre-filter eligible facts so invalid versions cannot crowd out current facts.
                rows = list(db.execute(
                    "SELECT fact_id,distance FROM memory_vec WHERE embedding MATCH ? AND k=? AND fact_id IN (" +
                    ",".join("?" for _ in ids) + ") ORDER BY distance",
                    (json.dumps(vector), min(len(ids), 64), *ids)))
            return ({row["fact_id"]: index + 1 for index, row in enumerate(rows)},
                    {row["fact_id"]: max(-1.0, min(1.0, 1 - row["distance"] ** 2 / 2)) for row in rows}, False)
        except Exception:
            return {}, {}, True

    @staticmethod
    def _rank(paths, records):
        ids = set().union(*(set(path) for path in paths))
        scores = {key: sum(1 / (60 + path[key]) for path in paths if key in path) for key in ids}
        return sorted(ids, key=lambda key: (-scores[key], -records[key]["confidence"], key)), scores

    def search(self, query, *, include_history=False, limit=8):
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Memory query must be nonempty text")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("Memory result limit must be a positive integer")
        limit = min(limit, 8)
        include_history = include_history or bool(re.search(
            r"\b(history|historical|former|previous|past|before)\b|when (did|was)|used to|以前|曾经|过去|变化|什么时候|\b\d{4}(?:-\d{2})?", query, re.I))
        rewritten = self._rewrite(query)
        vector_failed = self._sync_vectors()
        now = timestamp()
        with self._connect() as db:
            records = {row["fact_id"]: dict(row) for row in db.execute("SELECT * FROM memory_facts")
                       if row["status"] != "forgotten" and (include_history or current(row, now))}
        with ThreadPoolExecutor(max_workers=2) as pool:
            keyword = pool.submit(self._lexical, query + " " + rewritten, records)
            vector = pool.submit(self._vector, rewritten, records)
            lexical, lexical_quality = keyword.result()
            semantic, semantic_quality, failed = vector.result()
        vector_failed |= failed
        paths = [lexical, semantic]
        ranked, scores = self._rank(paths, records)
        best_quality = max([0.0, *lexical_quality.values(), *semantic_quality.values()])
        hyde_used = False
        if (len(ranked) < 3 or best_quality < 0.35) and self.rewrite_client is not None and self.embedding_client is not None:
            try:
                response = self.rewrite_client.complete([
                    {"role": "system", "content": "Generate a short hypothetical passage for vector retrieval, not a factual claim. Do not invent specific identifying details."},
                    {"role": "user", "content": query}], [], "none")
                hypothetical = response["content"].strip()
                if hypothetical:
                    hyde_used = True
                    extra, _, failed = self._vector(hypothetical, records)
                    vector_failed |= failed
                    paths.append(extra)
                    ranked, scores = self._rank(paths, records)
            except Exception:
                pass
        result, seen = [], set()
        with self._connect() as db:
            for key in ranked:
                # Recheck after external calls; concurrent forgetting must win.
                row = db.execute("SELECT * FROM memory_facts WHERE fact_id=?", (key,)).fetchone()
                if row is None or row["status"] == "forgotten" or (not include_history and not current(row, now)):
                    continue
                identity = (row["subject"], row["predicate"], row["object_normalized"])
                if include_history:
                    identity += (row["valid_from"], row["valid_to"])
                if identity in seen:
                    continue
                item = {field: row[field] for field in (
                    "fact_id", "subject", "predicate", "object", "text", "category",
                    "status", "confidence", "importance", "occurred_at", "recorded_at", "valid_from", "valid_to")}
                item.update(current=current(row, now), history=not current(row, now), relevance=scores[key])
                item["sources"] = [dict(source) for source in db.execute(
                    "SELECT source_task_id,source_event_id,trajectory_path,recorded_at,occurred_at,source_kind FROM fact_sources WHERE fact_id=? ORDER BY recorded_at,source_event_id", (key,))]
                # Never truncate an assertion into a different meaning. Skip an over-budget fact.
                if len(json.dumps(result + [item], ensure_ascii=False).encode("utf-8")) > 4000:
                    continue
                result.append(item)
                seen.add(identity)
                if len(result) == limit:
                    break
        return dict(query=query, rewritten_query=rewritten, facts=result, hyde_used=hyde_used,
                    vector_available=self._vec_available and self.embedding_client is not None and not vector_failed,
                    vector_failed=vector_failed, token_budget=1000)
