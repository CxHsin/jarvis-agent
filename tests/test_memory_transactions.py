"""Public memory operations retain one durable fact/profile outcome on failure."""

import sqlite3

import pytest

from memory_service import MemoryService
from stable_memory import timestamp


def fact(value):
    return dict(subject='USER', predicate='preferred_language', object=value,
                category='communication')


def source(value):
    return dict(quote=value, recorded_at='2026-01-01T00:00:00Z')


@pytest.mark.parametrize('action', ['correct', 'forget'])
def test_profile_database_failure_rolls_back_operation_and_allows_retry(tmp_path, action):
    memory = MemoryService(tmp_path)
    original = memory.remember(fact('Chinese'), source=source('Chinese'))
    memory.refresh_profile()
    before = memory.facts(True), memory.conflicts(), memory.profile_versions()
    markdown = memory.profile_path.read_text(encoding='utf-8')
    # Real SQLite trigger aborts after the fact change, at the version boundary.
    with sqlite3.connect(memory.path) as db:
        db.execute("""CREATE TRIGGER fail_profile BEFORE INSERT ON profile_versions
                      BEGIN SELECT RAISE(ABORT, 'profile write failed'); END""")

    def operate():
        if action == 'correct':
            return memory.correct(original, fact('English'), source=source('English'), operation_id='retry')
        return memory.forget(original, source=source('Forget Chinese'), operation_id='retry')

    with pytest.raises(sqlite3.IntegrityError, match='profile write failed'):
        operate()
    assert (memory.facts(True), memory.conflicts(), memory.profile_versions()) == before
    assert memory.profile_path.read_text(encoding='utf-8') == markdown
    assert [row['fact_id'] for row in memory.search('Chinese')['facts']] == [original]
    with sqlite3.connect(memory.path) as db:
        db.execute('DROP TRIGGER fail_profile')
    result = operate()
    assert operate() == result
    restarted = MemoryService(tmp_path)
    assert [row['object'] for row in restarted.facts()] == (['English'] if action == 'correct' else [])
    assert restarted.profile_snapshot()['content'] == restarted.profile_path.read_text(encoding='utf-8')
    assert len(restarted.profile_versions()) == len(before[2]) + 1


def test_pending_promotion_and_profile_intent_commit_together(tmp_path):
    import json
    from tests.test_memory_facts import StableModel

    memory = MemoryService(tmp_path)
    event = dict(type='task', task_id='task', event_id='event',
                 recorded_at=timestamp(), goal='I prefer concise responses')
    trajectory = tmp_path / 'evidence.jsonl'
    trajectory.write_text(json.dumps(event) + '\n', encoding='utf-8')
    memory.enqueue(dict(task_id='task', recorded_at=event['recorded_at'], events=[event]), trajectory)
    before = memory.profile_snapshot()
    with sqlite3.connect(memory.path) as db:
        db.execute("""CREATE TRIGGER fail_outbox BEFORE INSERT ON profile_publication
                      BEGIN SELECT RAISE(ABORT, 'outbox write failed'); END""")
    with pytest.raises(sqlite3.IntegrityError, match='outbox write failed'):
        memory.process_pending(StableModel())
    assert memory.facts(True) == []
    assert memory.conflicts() == []
    assert memory.pending_candidates()[0]['status'] == 'pending'
    assert memory.profile_snapshot() == before
    assert memory.search('concise')['facts'] == []
    with sqlite3.connect(memory.path) as db:
        db.execute('DROP TRIGGER fail_outbox')
    class Classifier:
        def complete(self, messages, tools, tool_choice):
            return {'content': json.dumps({'classification': dict(
                subject='USER', predicate='communication_style', object='concise',
                category='communication', confidence=0.95, explicit=True, stable=True,
                sensitive=False, inference=False, conflict='none')})}

    assert memory.consolidate(Classifier()) is True
    restarted = MemoryService(tmp_path)
    assert restarted.pending_candidates()[0]['status'] == 'promoted'
    assert restarted.facts()[0]['sources'][0]['quote'] == event['goal']
    assert 'concise' in restarted.task_prefix()


@pytest.mark.parametrize('action', ['remember', 'correct', 'forget'])
def test_late_operation_failure_keeps_facts_sources_conflicts_and_index_atomic(tmp_path, action):
    memory = MemoryService(tmp_path)
    original = memory.remember(fact('Chinese'), source=source('Chinese'))
    memory.refresh_profile()
    before = memory.facts(True), memory.conflicts(), memory.profile_versions()
    with sqlite3.connect(memory.path) as db:
        db.execute("""CREATE TRIGGER fail_operation BEFORE INSERT ON memory_operations
                      BEGIN SELECT RAISE(ABORT, 'operation write failed'); END""")

    def operate():
        if action == 'remember':
            return memory.remember(fact('English'), source=source('English'), operation_id='retry')
        if action == 'correct':
            return memory.correct(original, fact('English'), source=source('English'), operation_id='retry')
        return memory.forget(original, source=source('Forget Chinese'), operation_id='retry')

    with pytest.raises(sqlite3.IntegrityError, match='operation write failed'):
        operate()
    restarted = MemoryService(tmp_path)
    assert (restarted.facts(True), restarted.conflicts(), restarted.profile_versions()) == before
    assert restarted.search('English')['facts'] == []
    assert [row['fact_id'] for row in restarted.search('Chinese')['facts']] == [original]
    with sqlite3.connect(memory.path) as db:
        db.execute('DROP TRIGGER fail_operation')
    result = operate()
    assert operate() == result
    assert len(memory.facts(True)) == (1 if action == 'forget' else 2)


def test_correction_publication_recovers_without_overwriting_newer_manual_edit(tmp_path, monkeypatch):
    from pathlib import Path
    from memory_profile import ProfileEditError

    memory = MemoryService(tmp_path)
    original = memory.remember(fact('Chinese'), source=source('Chinese'))
    memory.refresh_profile()
    prior_file = memory.profile_path.read_text(encoding='utf-8')
    with monkeypatch.context() as fault:
        def interrupted(*args):
            raise OSError('publication interrupted')
        fault.setattr(Path, 'replace', interrupted)
        with pytest.raises(OSError, match='publication interrupted'):
            memory.correct(original, fact('English'), source=source('English'), operation_id='correction')
    assert [row['object'] for row in memory.facts()] == ['English']
    committed = memory.profile_snapshot()
    newer_edit = prior_file.replace('Chinese', 'French')
    memory.profile_path.write_text(newer_edit, encoding='utf-8')
    restarted = MemoryService(tmp_path)
    with pytest.raises(ProfileEditError, match='interrupted publication'):
        restarted.task_prefix()
    assert restarted.profile_path.read_text(encoding='utf-8') == newer_edit
    restarted.profile_path.write_text(committed['content'], encoding='utf-8')
    result = restarted.correct(original, fact('English'), source=source('English'), operation_id='correction')
    assert restarted.facts()[0]['fact_id'] == result
    assert restarted.profile_snapshot() == committed
    assert 'English' in restarted.task_prefix()


@pytest.mark.parametrize('boundary', ['empty_extraction', 'failed_consolidation'])
def test_remember_profile_waits_for_successful_publication_boundary(tmp_path, boundary):
    import json

    memory = MemoryService(tmp_path)
    if boundary == 'failed_consolidation':
        event = dict(type='task', task_id='task', event_id='event',
                     recorded_at=timestamp(), goal='I prefer concise responses')
        trajectory = tmp_path / 'evidence.jsonl'
        trajectory.write_text(json.dumps(event) + '\n', encoding='utf-8')
        memory.enqueue(dict(task_id='task', recorded_at=event['recorded_at'], events=[event]), trajectory)

        class Unclassified:
            def complete(self, messages, tools, tool_choice):
                return {'content': json.dumps({'candidates': [dict(
                    candidate_text='Prefers concise responses', source_event_ids=['event'])]})}

        memory.process_pending(Unclassified())
    memory.remember(fact('Chinese'), source=source('Chinese'))
    before = memory.profile_snapshot()
    assert 'Chinese' not in memory.task_prefix()
    if boundary == 'empty_extraction':
        memory.process_pending(None)
    else:
        class FailedModel:
            def complete(self, messages, tools, tool_choice):
                raise RuntimeError('classification unavailable')
        assert memory.consolidate(FailedModel()) is False
    assert memory.profile_snapshot() == before
    assert 'Chinese' not in memory.task_prefix()
    assert memory.facts()[0]['object'] == 'Chinese'
