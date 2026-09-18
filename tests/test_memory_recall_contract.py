from datetime import datetime, timedelta, timezone
import json

from memory_service import MemoryService
from tests.memory_helpers import wait_ready, wait_state


class Embeddings:
    def embed(self, text, model=None):
        return [0.0, 1.0, 0.0] if text == 'unrelated' else [1.0, 0.0, 0.0]


def source(text):
    return dict(quote=text, recorded_at=datetime.now(timezone.utc).isoformat(),
                source_task_id='task', source_event_id=text, trajectory_path='source.jsonl')


def remember(memory, value, **extra):
    return memory.remember(dict(subject='USER', predicate='likes', object=value,
                                text=value, category='work_preferences', **extra),
                           source=source(value))


def test_forgotten_fact_is_excluded_even_from_historical_recall(tmp_path):
    memory = MemoryService(tmp_path, embedding_client=Embeddings(), embedding_model="test", embedding_dimensions=3)
    try:
        fact = remember(memory, 'tea')
        memory.forget(fact, source=source('forget tea'))
        assert memory.search('tea', include_history=True)['facts'] == []
    finally:
        memory.close(wait=True)


def test_scheduled_future_change_does_not_hide_present_fact(tmp_path):
    memory = MemoryService(tmp_path, embedding_client=Embeddings(), embedding_model="test", embedding_dimensions=3)
    try:
        now = datetime.now(timezone.utc)
        def residence(city, effective):
            return memory.remember(dict(subject='USER', predicate='primary_residence',
                                        object=city, text='city ' + city, category='current_state',
                                        occurred_at=effective.isoformat()), source=source(city))
        present = residence('Shanghai', now - timedelta(days=30))
        future = residence('Beijing', now + timedelta(days=30))
        result = memory.search('city')['facts']
        assert {fact['fact_id'] for fact in result} == {present}
        assert future not in {fact['fact_id'] for fact in result}
    finally:
        memory.close(wait=True)


def test_three_semantically_weak_results_still_trigger_hyde(tmp_path):
    class QueryModel:
        def __init__(self):
            self.calls = []

        def complete(self, messages, tools, tool_choice):
            self.calls.append(messages)
            return {'content': json.dumps({'query': 'unrelated'}) if len(self.calls) == 1 else 'tea'}

    query_model = QueryModel()
    memory = MemoryService(tmp_path, embedding_client=Embeddings(),
                           rewrite_client=query_model, embedding_model="test", embedding_dimensions=3)
    try:
        for item in ('tea', 'coffee', 'water'):
            remember(memory, item)
        memory.start_worker(None)
        wait_ready(memory)
        memory.search('unrelated')
        assert len(query_model.calls) == 2
    finally:
        memory.close(wait=True)


def test_keyword_results_are_ranked_by_bm25_not_insertion_order(tmp_path):
    memory = MemoryService(tmp_path, embedding_dimensions=3)
    try:
        weak = memory.remember(dict(subject='USER', predicate='likes', object='weak',
                                    text='tea ' + 'other ' * 30, category='work_preferences'),
                               source=source('weak evidence'))
        strong = remember(memory, 'tea tea tea')
        results = memory.search('tea')['facts']
        assert {item['fact_id'] for item in results} == {weak, strong}
        assert results[0]['fact_id'] == strong
    finally:
        memory.close(wait=True)


def test_chinese_keyword_terms_work_without_vector_fallback(tmp_path):
    memory = MemoryService(tmp_path, embedding_dimensions=3)
    try:
        fact = remember(memory, '用户喜欢上午集中精力工作')
        assert [item['fact_id'] for item in memory.search('上午 工作')['facts']] == [fact]
    finally:
        memory.close(wait=True)


def test_embedding_outage_does_not_rollback_user_memory(tmp_path):
    class Unavailable:
        def embed(self, text, model=None):
            raise OSError('embedding service unavailable')

    memory = MemoryService(tmp_path, embedding_client=Unavailable(), embedding_model="test", embedding_dimensions=3)
    try:
        fact = remember(memory, 'tea')
        assert memory.facts()[0]['fact_id'] == fact
        memory.start_worker(None)
        wait_state(memory, 'retrying')
        result = memory.search('tea')
        assert result['vector_failed'] is True
        assert result['facts'][0]['fact_id'] == fact
    finally:
        memory.close(wait=True)


def test_enabling_embeddings_backfills_existing_facts(tmp_path):
    memory = MemoryService(tmp_path, embedding_dimensions=3)
    fact = remember(memory, 'tea')
    memory.close(wait=True)
    memory = MemoryService(tmp_path, embedding_client=Embeddings(), embedding_model="test", embedding_dimensions=3)
    try:
        memory.start_worker(None)
        wait_ready(memory)
        result = memory.search('semantic-only-query')
        assert result['vector_failed'] is False
        assert result['facts'][0]['fact_id'] == fact
    finally:
        memory.close(wait=True)


def test_recall_budget_counts_multibyte_facts_and_provenance_without_truncation(tmp_path):
    memory = MemoryService(tmp_path, embedding_dimensions=3)
    try:
        for index in range(12):
            remember(memory, '茶偏好' + str(index) + '细节' * 45)
        oversized = remember(memory, '茶' * 2000)
        results = memory.search('茶', limit=99)['facts']
        assert 0 < len(results) <= 8
        assert len(json.dumps(results, ensure_ascii=False).encode('utf-8')) <= 4000
        stored = {item['fact_id']: item for item in memory.facts()}
        assert oversized not in {item['fact_id'] for item in results}
        assert all(item['text'] == stored[item['fact_id']]['text'] and item['sources'] for item in results)
    finally:
        memory.close(wait=True)


def test_good_first_pass_skips_hyde_and_model_change_reembeds(tmp_path):
    class QueryModel:
        calls = 0
        def complete(self, messages, tools, tool_choice):
            self.calls += 1
            return {'content': '{"query":"tea"}'}
    class RecordingEmbeddings(Embeddings):
        calls = []
        def embed(self, text, model=None):
            self.calls.append((text, model))
            return super().embed(text, model)
    rewrite, embeddings = QueryModel(), RecordingEmbeddings()
    memory = MemoryService(tmp_path, embedding_client=embeddings, rewrite_client=rewrite,
                           embedding_dimensions=3, embedding_model='v1')
    try:
        for value in ('tea', 'coffee', 'water'):
            remember(memory, value)
        memory.start_worker(None)
        wait_ready(memory)
        assert memory.search('tea')['hyde_used'] is False
        assert rewrite.calls == 1
        memory.configure_retrieval(embedding_client=embeddings, rewrite_client=rewrite, embedding_model='v2')
        wait_ready(memory)
        memory.search('tea')
        assert all((text, 'v2') in embeddings.calls for text in ('tea', 'coffee', 'water'))
    finally:
        memory.close(wait=True)
