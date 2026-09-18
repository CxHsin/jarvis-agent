from concurrent.futures import ThreadPoolExecutor
from threading import Event
from tests.memory_helpers import wait_ready, wait_state
from memory_service import MemoryService
from tests.test_memory_recall_contract import remember
from application import Application
from configuration import Config
from tests.test_application import Client
from tests.test_memory_recall_contract import Embeddings, source
import pytest
import sqlite3


def test_slow_background_backfill_does_not_hold_search_or_fact_commit(tmp_path):
    entered, release = Event(), Event()

    class SlowEmbeddings:
        def embed(self, text, model=None):
            if text == 'tea':
                entered.set()
                assert release.wait(5)
            return [1.0, 0.0, 0.0]

    memory = MemoryService(tmp_path, embedding_client=SlowEmbeddings(),
                           embedding_model='test', embedding_dimensions=3)
    pool = ThreadPoolExecutor(1)
    try:
        tea = remember(memory, 'tea')
        memory.start_worker(None)
        assert entered.wait(2), 'application worker must backfill without a search'
        assert pool.submit(memory.search, 'tea').result(2)['facts'][0]['fact_id'] == tea
        coffee = pool.submit(remember, memory, 'coffee').result(2)
        assert memory.search('coffee')['facts'][0]['fact_id'] == coffee
        release.set()
        wait_ready(memory)
        assert {f['fact_id'] for f in memory.search('semantic-only-query')['facts']} == {tea, coffee}
    finally:
        release.set()
        memory.close(wait=True)
        pool.shutdown()


def test_keyword_commit_is_searchable_without_foreground_backfill(tmp_path):
    class Embeddings:
        def __init__(self):
            self.calls = []

        def embed(self, text, model=None):
            self.calls.append(text)
            raise OSError('offline')

    client = Embeddings()
    memory = MemoryService(tmp_path, embedding_client=client,
                           embedding_model='test', embedding_dimensions=3)
    try:
        fact = remember(memory, 'tea')
        result = memory.search('tea preference')
        assert result['facts'][0]['fact_id'] == fact
        assert result['facts'][0]['sources'][0]['source_event_id'] == 'tea'
        assert client.calls == [], 'no usable vectors means no embedding or HyDE requests'
        assert result['vector_available'] is False
    finally:
        memory.close(wait=True)


@pytest.mark.parametrize('settings, state, missing', [
    ({}, 'disabled', {'base_url', 'api_key', 'model', 'dimensions'}),
    ({'embedding_base_url': 'http://example.test', 'embedding_model': 'test'},
     'incomplete', {'api_key', 'dimensions'}),
    ({'embedding_api_key': 'test', 'embedding_dimensions': 3},
     'incomplete', {'base_url', 'model'}),
])
def test_application_reports_incomplete_embedding_without_network(tmp_path, settings, state, missing):
    config = Config('http://example.test', '', 'chat', root_dir=tmp_path,
                    state_dir=tmp_path / 'state', **settings)
    with Application(config, client=Client(), extraction_client=Client()) as app:
        session = app.create_session()
        fact = remember(app.memory, 'tea')
        result = app.memory.search('tea')
        assert result['facts'][0]['fact_id'] == fact
        assert result['vector_available'] is False
        assert result['hyde_used'] is False
        assert result['vector_status']['state'] == state
        assert set(result['vector_status']['missing']) == missing
        session.close()


def test_ready_vectors_survive_disabled_restart_and_failed_new_backfill(tmp_path):
    memory = MemoryService(tmp_path, embedding_client=Embeddings(),
                           embedding_model='test', embedding_dimensions=3)
    tea = remember(memory, 'tea')
    memory.start_worker(None)
    wait_ready(memory)
    memory.close(wait=True)
    disabled = MemoryService(tmp_path)
    assert disabled.search('tea')['facts'][0]['fact_id'] == tea
    disabled.close(wait=True)
    failed = Event()

    class PartialOutage(Embeddings):
        def embed(self, text, model=None):
            assert text != 'tea', 'restart must reuse the persisted ready vector'
            if text == 'coffee':
                failed.set()
                raise OSError('offline')
            return super().embed(text, model)

    memory = MemoryService(tmp_path, embedding_client=PartialOutage(),
                           embedding_model='test', embedding_dimensions=3)
    try:
        remember(memory, 'coffee')
        memory.start_worker(None, retry_seconds=0.01)
        assert failed.wait(2)
        wait_state(memory, 'retrying')
        result = memory.search('semantic-only-query')
        assert result['vector_available'] is True
        assert {f['fact_id'] for f in result['facts']} == {tea}
        assert memory.search('coffee')['facts'][0]['object'] == 'coffee'
    finally:
        memory.close(wait=True)


def test_reconfigured_client_rejects_old_inflight_result_even_for_same_model(tmp_path):
    entered, release, replacement_called = Event(), Event(), Event()

    class OldClient(Embeddings):
        def embed(self, text, model=None):
            entered.set()
            assert release.wait(5)
            return [0.0, 1.0, 0.0]

    class Replacement(Embeddings):
        def embed(self, text, model=None):
            replacement_called.set()
            return super().embed(text, model)

    memory = MemoryService(tmp_path, embedding_client=OldClient(),
                           embedding_model='test', embedding_dimensions=3)
    try:
        remember(memory, 'tea')
        memory.start_worker(None)
        assert entered.wait(2)
        memory.configure_retrieval(embedding_client=Replacement(), embedding_model='test')
        release.set()
        assert replacement_called.wait(2), 'old in-flight result must not satisfy new configuration'
        wait_ready(memory)
    finally:
        release.set()
        memory.close(wait=True)


def test_model_change_rejects_inflight_backfill_and_retries_new_model(tmp_path):
    entered, release = Event(), Event()

    class Controlled(Embeddings):
        def embed(self, text, model=None):
            if model == 'old':
                entered.set()
                assert release.wait(5)
                return [0.0, 1.0, 0.0]
            return super().embed(text, model)

    client = Controlled()
    memory = MemoryService(tmp_path, embedding_client=client,
                           embedding_model='old', embedding_dimensions=3)
    try:
        tea = remember(memory, 'tea')
        memory.start_worker(None)
        assert entered.wait(2)
        memory.configure_retrieval(embedding_client=client, embedding_model='new')
        release.set()
        wait_ready(memory)
        result = memory.search('semantic-only-query')
        assert result['facts'][0]['fact_id'] == tea
        assert result['vector_available'] is True
    finally:
        release.set()
        memory.close(wait=True)


def test_enabling_running_service_retries_outage_then_restart_is_idempotent(tmp_path):
    failing = Event()
    recovered = Event()
    class Recovering(Embeddings):
        calls = 0
        def embed(self, text, model=None):
            if text == 'tea':
                self.calls += 1
                if not recovered.is_set():
                    failing.set()
                    raise OSError('offline')
            return super().embed(text, model)

    client = Recovering()
    memory = MemoryService(tmp_path, embedding_dimensions=3)
    try:
        tea = remember(memory, 'tea')
        memory.start_worker(None, retry_seconds=0.01)
        memory.configure_retrieval(embedding_client=client, embedding_model='test')
        assert failing.wait(2)
        assert memory.search('tea')['facts'][0]['fact_id'] == tea
        recovered.set()
        wait_ready(memory)
    finally:
        recovered.set()
        memory.close(wait=True)
    calls = client.calls
    memory = MemoryService(tmp_path, embedding_client=client,
                           embedding_model='test', embedding_dimensions=3)
    try:
        memory.start_worker(None, retry_seconds=0.01)
        wait_ready(memory)
        assert memory.search('semantic-only-query')['facts'][0]['fact_id'] == tea
    finally:
        memory.close(wait=True)
    assert client.calls == calls


def test_forgotten_fact_rejects_late_backfill_and_survives_restart(tmp_path):
    entered, release = Event(), Event()
    class Controlled(Embeddings):
        def embed(self, text, model=None):
            entered.set()
            assert release.wait(5)
            return super().embed(text, model)

    memory = MemoryService(tmp_path, embedding_client=Controlled(),
                           embedding_model='test', embedding_dimensions=3)
    try:
        tea = remember(memory, 'tea')
        memory.start_worker(None)
        assert entered.wait(2)
        memory.forget(tea, source=source('forget tea'))
        release.set()
        wait_ready(memory)
    finally:
        release.set()
        memory.close(wait=True)
    memory = MemoryService(tmp_path, embedding_client=Embeddings(),
                           embedding_model='test', embedding_dimensions=3)
    try:
        assert memory.vector_status()['ready'] == 0
        assert memory.search('semantic-only-query', include_history=True)['facts'] == []
    finally:
        memory.close(wait=True)


@pytest.mark.parametrize('change', ['content', 'model', 'dimensions'])
def test_mismatched_persisted_vectors_are_excluded_until_rebuilt(tmp_path, change):
    memory = MemoryService(tmp_path, embedding_client=Embeddings(),
                           embedding_model='old', embedding_dimensions=3)
    tea = remember(memory, 'tea')
    memory.start_worker(None)
    wait_ready(memory)
    memory.close(wait=True)
    if change == 'content':
        # A persisted stale-index fixture, observed only via public recall/status.
        with sqlite3.connect(tmp_path / 'memory.db') as db:
            db.execute('UPDATE memory_facts SET text=? WHERE fact_id=?', ('new tea detail', tea))
    dimensions = 2 if change == 'dimensions' else 3
    class NewEmbeddings:
        def embed(self, text, model=None):
            return [1.0] + [0.0] * (dimensions - 1)
    memory = MemoryService(tmp_path, embedding_client=NewEmbeddings(),
                           embedding_model='new' if change == 'model' else 'old',
                           embedding_dimensions=dimensions)
    try:
        assert memory.vector_status()['ready'] == 0
        assert memory.search('semantic-only-query')['facts'] == []
        assert memory.search('tea')['facts'][0]['fact_id'] == tea
        memory.start_worker(None)
        wait_ready(memory)
        assert memory.search('semantic-only-query')['facts'][0]['fact_id'] == tea
    finally:
        memory.close(wait=True)


def test_application_owns_backfill_after_one_session_closes(tmp_path):
    config = Config('http://example.test', '', 'chat', root_dir=tmp_path,
                    state_dir=tmp_path / 'state', embedding_model='test', embedding_dimensions=3)
    with Application(config, client=Client(), extraction_client=Client(),
                     embedding_client=Embeddings()) as app:
        first, second = app.create_session(), app.create_session()
        remember(app.memory, 'tea')
        wait_ready(app.memory)
        first.close()
        coffee = remember(second.store.memory, 'coffee')
        wait_ready(app.memory)
        assert coffee in {f['fact_id'] for f in app.memory.search('semantic-only-query')['facts']}


def test_stop_discards_inflight_result_and_restart_finishes_gap(tmp_path):
    entered, release = Event(), Event()
    class Controlled(Embeddings):
        def embed(self, text, model=None):
            entered.set()
            assert release.wait(5)
            return super().embed(text, model)

    memory = MemoryService(tmp_path, embedding_client=Controlled(),
                           embedding_model='test', embedding_dimensions=3)
    try:
        tea = remember(memory, 'tea')
        memory.start_worker(None)
        assert entered.wait(2)
        memory.close()
    finally:
        release.set()
        memory.close(wait=True)

    memory = MemoryService(tmp_path, embedding_client=Embeddings(),
                           embedding_model='test', embedding_dimensions=3)
    try:
        assert memory.vector_status()['pending'] == 1
        memory.start_worker(None)
        wait_ready(memory)
        assert memory.search('semantic-only-query')['facts'][0]['fact_id'] == tea
    finally:
        memory.close(wait=True)


def test_model_change_during_hyde_discards_old_semantic_ranking(tmp_path):
    class Unavailable:
        def embed(self, text, model=None):
            raise OSError('offline')

    class Rewrite:
        def complete(self, messages, tools, tool_choice):
            if 'hypothetical' in messages[0]['content']:
                memory.configure_retrieval(embedding_client=Unavailable(), embedding_model='new')
                return {'content': 'a hypothetical drink'}
            return {'content': '{"query":"semantic-only-query"}'}

    memory = MemoryService(tmp_path, embedding_client=Embeddings(), rewrite_client=Rewrite(),
                           embedding_model='old', embedding_dimensions=3)
    try:
        remember(memory, 'tea')
        memory.start_worker(None)
        wait_ready(memory)
        result = memory.search('semantic-only-query')
        assert result['facts'] == []
        assert result['vector_available'] is False
    finally:
        memory.close(wait=True)
