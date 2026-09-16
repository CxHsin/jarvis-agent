import json

from session_store import SessionStore
from tests.test_session_persistence import SessionTestBase
from jarvis_agent import Agent
from tests.test_jarvis_agent import FakeClient
from contextlib import redirect_stdout
from io import StringIO
import time


class StableModel:
    def __init__(self, **overrides):
        self.overrides = overrides

    def complete(self, messages, tools, tool_choice):
        events = json.loads(messages[-1]['content'])
        event = events[0]
        candidate = dict(candidate_text='Prefers concise responses',
                         source_event_ids=[event['event_id']], occurred_at=None,
                         subject='USER', predicate='communication_style', object='concise',
                         category='communication', confidence=0.95, importance=0.8,
                         explicit=True, stable=True, sensitive=False, remember_consent=False,
                         inference=False, conflict='none')
        candidate.update(self.overrides)
        return {'content': json.dumps({'candidates': [candidate]})}


class MemoryFactsTests(SessionTestBase):
    def test_corrected_nonexclusive_assertion_cannot_return_from_old_evidence(self):
        with SessionStore.create(self.config(recent_task_count=1)) as store:
            store.record_task(1, 'I prefer concise responses')
            store.end_task()
            store.record_task(2, 'next')
            store.end_task()
            store.memory.process_pending(StableModel())
            original = store.memory.facts()[0]
            from stable_memory import timestamp
            corrected = store.memory.correct(original['fact_id'], dict(subject='USER',
                predicate='communication_style', object='detailed', category='communication'),
                source=self.source('Correction: detailed', timestamp(), 'correction'))
            store.record_task(3, 'next')
            store.end_task()
            store.memory.process_pending(StableModel(candidate_text='Enjoys concise replies'))
            self.assertEqual([fact['fact_id'] for fact in store.memory.facts()], [corrected])

    def fact(self, object, **updates):
        return dict(subject='USER', predicate='primary_residence', object=object,
                    category='current_state', **updates)

    def source(self, text, date='2026-01-01T10:00:00+08:00', event='e1'):
        return dict(source_task_id='task', source_event_id=event, quote=text,
                    recorded_at=date, trajectory_path='user-operation')

    def test_stable_fact_promotes_with_original_provenance_and_survives_restart(self):
        with SessionStore.create(self.config(recent_task_count=1)) as store:
            store.record_task(1, 'I prefer concise responses')
            store.end_task()
            store.record_task(2, 'next')
            store.end_task()
            store.memory.process_pending(StableModel())
            facts = store.memory.facts()
            self.assertEqual(len(facts), 1)
            fact = facts[0]
            self.assertEqual(fact['subject'], 'USER')
            self.assertEqual(fact['object'], 'concise')
            self.assertIsNone(fact['occurred_at'])
            self.assertEqual(fact['recorded_at'], fact['sources'][0]['recorded_at'])
            self.assertLess(fact['recorded_at'], fact['created_at'])
            self.assertEqual(fact['sources'][0]['quote'], 'I prefer concise responses')
            self.assertEqual(store.memory.pending_candidates()[0]['status'], 'promoted')
            session_id = store.session_id
        with SessionStore.resume(self.config(), session_id) as store:
            store.memory.process_pending(StableModel())
            self.assertEqual(store.memory.facts(), facts)

    def test_exclusive_fact_history_late_evidence_and_explicit_correction(self):
        with SessionStore.create(self.config()) as store:
            memory = store.memory
            first = memory.remember(self.fact('Shanghai'), source=self.source('I live in Shanghai'), operation_id='one')
            newest = memory.remember(self.fact('Beijing'), source=self.source('I moved to Beijing', '2026-03-01T00:00:00Z', 'e3'))
            middle = memory.remember(self.fact('Nanjing'), source=self.source('I lived in Nanjing', '2026-02-01T00:00:00Z', 'e2'))
            self.assertEqual([f['fact_id'] for f in memory.facts()], [newest])
            timeline = {f['fact_id']: f for f in memory.facts(include_inactive=True)}
            self.assertEqual(timeline[first]['valid_to'], '2026-02-01T00:00:00.000000+00:00')
            self.assertEqual(timeline[middle]['valid_to'], '2026-03-01T00:00:00.000000+00:00')
            corrected = memory.correct(newest, self.fact('Chengdu'),
                source=self.source('Correction: Chengdu', '2026-03-02T00:00:00Z', 'e4'), operation_id='correction')
            self.assertEqual(memory.facts()[0]['fact_id'], corrected)
            self.assertEqual(memory.facts()[0]['source_kind'], 'user_correction')
            self.assertEqual(memory.remember(self.fact('Shanghai'), source=self.source('I live in Shanghai'), operation_id='one'), first)
            self.assertEqual(len(memory.facts(include_inactive=True)), 4)
            self.assertTrue(all(f['invalidation_reason'] for f in memory.facts(include_inactive=True) if f['status'] != 'active'))

    def test_admission_conflicts_correction_priority_and_coexisting_likes(self):
        with SessionStore.create(self.config(recent_task_count=1)) as store:
            for index, overrides in enumerate([
                dict(candidate_text='Likes tea', predicate='likes', object='tea', category='work_preferences'),
                dict(candidate_text='Likes coffee', predicate='likes', object='coffee', category='work_preferences'),
                dict(candidate_text='Inferred preference', inference=True),
                dict(candidate_text='Temporary plan', stable=False),
                dict(candidate_text='Private health', sensitive=True),
                dict(candidate_text='Uncertain preference', conflict='uncertain'),
                dict(candidate_text='Explicit health memory', sensitive=True, remember_consent=True,
                     predicate='health_constraint', object='avoid peanuts', category='constraints'),
            ]):
                store.record_task(index * 2, overrides['candidate_text'])
                store.end_task()
                store.record_task(index * 2 + 1, 'next')
                store.end_task()
                store.memory.process_pending(StableModel(**overrides))
            self.assertEqual({f['object'] for f in store.memory.facts()}, {'tea', 'coffee', 'avoid peanuts'})
            pending = [c for c in store.memory.pending_candidates() if c['status'] == 'pending']
            self.assertEqual(len(pending), 4)
            self.assertTrue(all(c['reason'] for c in pending))
            current = store.memory.remember(self.fact('Paris'), source=self.source('Paris'))
            corrected = store.memory.correct(current, self.fact('London'), source=self.source('Correction London', event='correct'))
            store.record_task(20, 'Maybe Paris again')
            store.end_task()
            store.record_task(21, 'next')
            store.end_task()
            store.memory.process_pending(StableModel(candidate_text='Lives in Paris', predicate='primary_residence', object='Paris', category='current_state'))
            self.assertIn(corrected, [f['fact_id'] for f in store.memory.facts()])
            self.assertTrue(any(c['status'] == 'pending' and 'correction' in (c['reason'] or '') for c in store.memory.pending_candidates()))

    def test_forget_prevents_same_old_evidence_resurrection_and_preserves_audit(self):
        with SessionStore.create(self.config(recent_task_count=1)) as store:
            store.record_task(1, 'I prefer concise responses')
            store.end_task()
            store.record_task(2, 'I also prefer concise responses')
            store.end_task()
            store.memory.process_pending(StableModel())
            fact_id = store.memory.facts()[0]['fact_id']
            from stable_memory import timestamp
            store.memory.forget(fact_id, source=self.source('Forget that', timestamp(), 'forget'), operation_id='forget')
            store.record_task(3, 'next')
            store.end_task()
            store.memory.process_pending(StableModel(candidate_text='Enjoys brief replies'))
            self.assertEqual(store.memory.facts(), [])
            self.assertEqual(store.memory.facts(include_inactive=True)[0]['status'], 'forgotten')
            self.assertTrue(any(d['decision'] == 'suppressed' for d in store.memory.conflicts()))

    def test_agent_extracts_only_after_recent_eviction(self):
        with redirect_stdout(StringIO()):
            agent = Agent(self.config(recent_task_count=1), FakeClient([{'role': 'assistant', 'content': 'ok'}] * 2),
                          extraction_client=StableModel())
            self.agents.append(agent)
            agent.run_request('I prefer concise responses')
            agent.store.memory._recover_completed()
            self.assertEqual(agent.store.memory.pending_batches(), [])
            self.assertEqual(agent.store.memory.facts(), [])
            agent.run_request('Next task')
            deadline = time.monotonic() + 3
            while not agent.store.memory.facts() and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(len(agent.store.memory.facts()), 1)

    def test_nightly_consolidation_catches_up_legacy_candidates_after_restart(self):
        with SessionStore.create(self.config(recent_task_count=1)) as store:
            store.record_task(1, 'I prefer concise responses')
            store.end_task()
            store.record_task(2, 'next')
            store.end_task()
            batch = store.memory.pending_batches()[0]
            store.memory.process_pending(FakeClient([{'content': json.dumps({'candidates': [{
                'candidate_text': 'Prefers concise responses', 'source_event_ids': [batch['source_event_ids'][0]],
            }]})}]))
            session_id = store.session_id
        class Classifier:
            def complete(self, messages, tools, tool_choice):
                return {'content': json.dumps({'classification': dict(subject='USER', predicate='communication_style',
                    object='concise', category='communication', confidence=0.9, importance=0.7, explicit=True,
                    stable=True, sensitive=False, remember_consent=False, inference=False, conflict='none')})}
        with SessionStore.resume(self.config(), session_id) as store:
            self.assertTrue(store.memory.consolidate_due(Classifier()))
            self.assertEqual(len(store.memory.facts()), 1)
            self.assertFalse(store.memory.consolidate_due(Classifier()))

    def test_duplicate_adds_sources_and_unknown_exclusivity_stays_pending(self):
        with SessionStore.create(self.config(recent_task_count=1)) as store:
            store.record_task(1, 'I prefer concise responses')
            store.end_task()
            store.record_task(2, 'I still prefer concise responses')
            store.end_task()
            store.memory.process_pending(StableModel())
            original_id = store.memory.facts()[0]['fact_id']
            store.record_task(3, 'I now prefer detailed responses')
            store.end_task()
            store.memory.process_pending(StableModel(object=' CONCISE '))
            self.assertEqual(len(store.memory.facts()), 1)
            self.assertEqual(store.memory.facts()[0]['fact_id'], original_id)
            self.assertEqual(len(store.memory.facts()[0]['sources']), 2)
            store.record_task(4, 'next')
            store.end_task()
            store.memory.process_pending(StableModel(candidate_text='Prefers detailed responses', object='detailed', conflict='factual'))
            self.assertEqual(len(store.memory.facts()), 1)
            pending = next(c for c in store.memory.pending_candidates() if c['status'] == 'pending')
            self.assertIn('Unknown exclusivity', pending['reason'])

    def test_same_time_correction_and_failed_operation_are_atomic(self):
        with SessionStore.create(self.config()) as store:
            memory = store.memory
            original = memory.remember(self.fact('Paris'), source=self.source('Paris'))
            corrected = memory.correct(original, self.fact('London', occurred_at='2025-12-01T00:00:00Z'),
                source=self.source('Correction London', event='correct'), operation_id='correction')
            self.assertEqual([f['fact_id'] for f in memory.facts()], [corrected])
            snapshot = memory.facts(include_inactive=True)
            with self.assertRaises(ValueError):
                memory.correct(corrected, dict(subject='USER', predicate='likes', object='tea', category='work_preferences'),
                               source=self.source('Wrong attribute', event='bad'))
            self.assertEqual(memory.facts(include_inactive=True), snapshot)

    def test_late_retry_of_older_duplicate_keeps_both_sources(self):
        class FailFirst(StableModel):
            calls = 0
            def complete(self, messages, tools, tool_choice):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError('temporary upstream error')
                return super().complete(messages, tools, tool_choice)
        with SessionStore.create(self.config(recent_task_count=1)) as store:
            for index in range(3):
                store.record_task(index, 'I prefer concise responses')
                store.end_task()
            store.memory.process_pending(FailFirst())
            self.assertEqual(len(store.memory.facts()[0]['sources']), 1)
            store.memory.process_pending(StableModel())
            self.assertEqual(len(store.memory.facts()[0]['sources']), 2)

    def test_future_effective_state_does_not_hide_current_fact_early(self):
        with SessionStore.create(self.config()) as store:
            current = store.memory.remember(self.fact('Paris'), source=self.source('Paris'))
            store.memory.remember(self.fact('London', occurred_at='2099-01-01T00:00:00Z'),
                                  source=self.source('Future confirmed residence', event='future'))
            self.assertEqual([f['fact_id'] for f in store.memory.facts()], [current])
