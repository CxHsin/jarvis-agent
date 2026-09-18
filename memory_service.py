"""Durable derived memory; original evidence remains in task trajectories."""

import json
from pathlib import Path
import urllib.request
from stable_memory import StableMemory
from memory_retrieval import MemoryRetrieval
from memory_profile import ProfileMemory
from memory_store import MemoryStore
from memory_pending import PendingMemory


class OpenAIEmbeddingClient:
    """Small OpenAI-compatible embeddings adapter; callers may inject a fake client."""
    def __init__(self, endpoint, api_key, model, timeout=60):
        self.endpoint = endpoint.rstrip('/') + '/embeddings'
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def embed(self, text, model=None):
        request = urllib.request.Request(self.endpoint,
            data=json.dumps({'model': model or self.model, 'input': text}).encode(),
            headers={'Content-Type': 'application/json', **({'Authorization': 'Bearer ' + self.api_key} if self.api_key else {})},
            method='POST')
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode())
        return payload['data'][0]['embedding']


class MemoryService:
    """Public memory boundary and explicit composition root.

    Fact/index changes share one transaction. Corrections and forgetting also
    stage the profile version/outbox there, then publish the committed file.
    Remember retains its existing deferred profile publication contract.
    """

    def __init__(self, directory: Path, *, embedding_client=None, rewrite_client=None,
                 embedding_model=None, embedding_dimensions=None, embedding_configuration=None):
        self._store = MemoryStore(directory)
        self.directory = self._store.directory
        self.path = self._store.path
        self._retrieval = MemoryRetrieval(self._store, embedding_client=embedding_client,
            rewrite_client=rewrite_client, embedding_model=embedding_model,
            embedding_dimensions=embedding_dimensions, embedding_configuration=embedding_configuration)
        self._facts = StableMemory(self._store, self._retrieval.index_fact)
        self._profile = ProfileMemory(self._store, self._facts)
        self._pending = PendingMemory(self._store, self._facts, self._profile)
        self.profile_path = self._profile.profile_path
        self.self_path = self._profile.self_path
        self.pending_path = self._pending.pending_path
        with self._store.transaction() as db:
            self._pending.initialize(db)
            self._facts.initialize(db)
            self._profile.initialize(db)
            self._retrieval.initialize(db)
        self._pending.project()

    @property
    def embedding_client(self):
        """Retain the host's configured embedding adapter across session resume."""
        return self._retrieval.embedding_client

    @property
    def extraction_enabled(self):
        return self._pending.extraction_enabled

    def remember(self, fact, *, source, operation_id=None):
        """An explicit user request, including consent; publish at consolidation."""
        return self._operate('remember', fact, source, operation_id)

    def correct(self, fact_id, fact, *, source, operation_id=None):
        return self._operate('correct', fact, source, operation_id, fact_id)

    def forget(self, fact_id, *, source, operation_id=None):
        return self._operate('forget', None, source, operation_id, fact_id)

    def _operate(self, action, fact, source, operation_id, target=None):
        transaction = self._store.transaction() if action == 'remember' else self._profile.fact_transaction()
        with transaction as db:
            result = self._facts.operate(db, action, fact, source, operation_id, target)
        self._pending.project()
        return result

    def facts(self, include_inactive=False):
        return self._facts.facts(include_inactive)

    def conflicts(self):
        return self._facts.conflicts()

    def search(self, query, *, include_history=False, limit=8):
        return self._retrieval.search(query, include_history=include_history, limit=limit)

    def vector_status(self):
        return self._retrieval.vector_status()

    def configure_retrieval(self, *, embedding_client=None, rewrite_client=None, embedding_model=None):
        self._retrieval.configure_retrieval(embedding_client=embedding_client,
            rewrite_client=rewrite_client, embedding_model=embedding_model)

    def task_prefix(self):
        return self._profile.task_prefix()

    def prefix_snapshot(self):
        return self._profile.prefix_snapshot()

    def profile_snapshot(self):
        return self._profile.profile_snapshot()

    def profile_versions(self):
        return self._profile.profile_versions()

    def import_profile_edits(self):
        return self._profile.import_profile_edits()

    def refresh_profile(self):
        return self._profile.refresh_profile()

    def enqueue(self, task, trajectory_path):
        self._pending.enqueue(task, trajectory_path)

    def pending_batches(self):
        return self._pending.pending_batches()

    def pending_candidates(self):
        return self._pending.pending_candidates()

    def process_pending(self, client):
        self._pending.process_pending(client)

    def expire_candidates(self, now=None):
        self._pending.expire_candidates(now)

    def consolidate(self, client):
        return self._pending.consolidate(client)

    def consolidate_due(self, client, now=None):
        return self._pending.consolidate_due(client, now)

    def start_worker(self, client, retry_seconds=60):
        self._retrieval.start_worker(retry_seconds)
        if client is not None:
            self._pending.start_worker(client, retry_seconds)

    def _recover_completed(self):
        # Compatibility for existing host recovery callers.
        self._pending.recover_completed()

    def close(self, *, wait=False):
        self._retrieval.close(wait=wait)
        self._pending.close(wait=wait)
