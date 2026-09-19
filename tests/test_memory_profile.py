import json

from session.session_store import SessionStore
from tests.test_session_persistence import SessionTestBase
from tests.test_jarvis_agent import FakeClient
from agent.agent import Agent
from contextlib import redirect_stdout
from io import StringIO
from memory.memory_profile import ProfileEditError


class MemoryProfileTests(SessionTestBase):
    def test_profile_is_versioned_bounded_and_changes_only_at_consolidation(self):
        with SessionStore.create(self.config()) as store:
            memory = store.memory
            original = memory.profile_snapshot()
            fact_id = memory.remember(dict(subject='USER', predicate='preferred_language',
                object='Chinese', category='communication'),
                source=dict(quote='Please remember Chinese', recorded_at='2026-01-01T00:00:00Z'))
            self.assertEqual(memory.profile_snapshot(), original)
            self.assertTrue(memory.consolidate(None))
            profile = memory.profile_snapshot()
            self.assertIn('Chinese', profile['content'])
            self.assertIn('## communication', profile['content'])
            self.assertNotIn('## identity', profile['content'])
            self.assertEqual(profile['source_fact_ids'], [fact_id])
            self.assertLessEqual(profile['token_count'], 1500)
            memory.consolidate(None)
            self.assertEqual(memory.profile_snapshot(), profile)
            self.assertEqual(len(memory.profile_versions()), 2)

    def test_agent_freezes_self_and_profile_until_next_task(self):
        class EditingClient(FakeClient):
            def complete(client, messages, tools, tool_choice):
                result = super().complete(messages, tools, tool_choice)
                if len(client.calls) == 1:
                    memory.self_path.write_text('Changed self', encoding='utf-8')
                    memory.remember(dict(subject='USER', predicate='likes', object='tea', category='work_preferences'),
                        source=dict(quote='I like tea', recorded_at='2026-01-01T00:00:00Z'))
                    memory.consolidate(None)
                return result
        client = EditingClient([
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'call1', 'type': 'function',
                'function': {'name': 'list_directory', 'arguments': '{"path":"."}'}}]},
            {'role': 'assistant', 'content': 'done'}, {'role': 'assistant', 'content': 'next'},
        ])
        with redirect_stdout(StringIO()):
            agent = Agent(self.config(), client, extraction_client=FakeClient([]))
            self.agents.append(agent)
            memory = agent.store.memory
            memory.self_path.write_text('Original self', encoding='utf-8')
            agent.run_request('first')
            agent.run_request('second')
        prompts = [request[0][0]['content'] for request in client.requests]
        self.assertEqual(prompts[0], prompts[1])
        self.assertIn('Original self', prompts[0])
        self.assertNotIn('tea', prompts[0])
        self.assertIn('Changed self', prompts[2])
        self.assertIn('tea', prompts[2])

    def test_structured_user_edit_corrects_and_forgets_with_auditable_sources(self):
        with SessionStore.create(self.config()) as store:
            memory = store.memory
            original = memory.remember(dict(subject='USER', predicate='preferred_language', object='Chinese', category='communication'),
                source=dict(quote='Use Chinese', recorded_at='2026-01-01T00:00:00Z'))
            memory.consolidate(None)
            edited = memory.profile_path.read_text(encoding='utf-8').replace('Chinese', 'English')
            memory.profile_path.write_text(edited, encoding='utf-8')
            prefix = memory.task_prefix()
            self.assertIn('English', prefix)
            fact = memory.facts()[0]
            self.assertEqual(fact['object'], 'English')
            self.assertEqual(fact['confidence'], 1.0)
            self.assertEqual(fact['source_kind'], 'user_correction')
            self.assertEqual(fact['sources'][0]['quote'], edited)
            old = next(f for f in memory.facts(True) if f['fact_id'] == original)
            self.assertEqual(old['status'], 'invalidated')
            self.assertEqual(old['sources'][0]['quote'], 'Use Chinese')
            memory.profile_path.write_text('# User profile\n\n', encoding='utf-8')
            memory.task_prefix()
            self.assertEqual(memory.facts(), [])
            self.assertEqual(next(f for f in memory.facts(True) if f['fact_id'] == fact['fact_id'])['status'], 'forgotten')

    def test_invalid_edits_are_preserved_and_do_not_partially_import(self):
        with SessionStore.create(self.config()) as store:
            memory = store.memory
            edited = '# User profile\n\n## identity\n\n- ' + json.dumps(dict(fact_id=None,
                predicate='preferred_name', object='Alice')) + '\nthis is not a structured fact\n'
            memory.profile_path.write_text(edited, encoding='utf-8')
            with self.assertRaisesRegex(ProfileEditError, 'Invalid memory.md row'):
                memory.task_prefix()
            self.assertEqual(memory.facts(True), [])
            with self.assertRaises(ProfileEditError):
                memory.consolidate(None)
            self.assertEqual(memory.profile_path.read_text(encoding='utf-8'), edited)
            self.assertEqual(len(memory.profile_versions()), 1)

    def test_profile_bound_omits_whole_facts_but_database_retains_them(self):
        with SessionStore.create(self.config()) as store:
            memory = store.memory
            for index in range(40):
                memory.remember(dict(subject='USER', predicate='likes', object=f'{index}: ' + '偏好' * 70,
                    category='work_preferences'), source=dict(quote='Explicit preference', recorded_at='2026-01-01T00:00:00Z'))
            memory.consolidate(None)
            profile = memory.profile_snapshot()
            self.assertEqual(len(memory.facts()), 40)
            self.assertGreater(len(profile['source_fact_ids']), 0)
            self.assertLess(len(profile['source_fact_ids']), 40)
            self.assertLessEqual(profile['token_count'], 1500)
            self.assertLessEqual(len(profile['content'].encode('utf-8')), 6000)
            for line in profile['content'].splitlines():
                if line.startswith('- '):
                    self.assertIn(json.loads(line[2:])['fact_id'], profile['source_fact_ids'])

    def test_new_structured_edit_is_shared_across_projects_and_survives_restart(self):
        with SessionStore.create(self.config()) as store:
            memory = store.memory
            memory.profile_path.write_text('# User profile\n\n## identity\n\n- ' +
                json.dumps(dict(fact_id=None, predicate='preferred_name', object='Alice')) + '\n', encoding='utf-8')
            self.assertIn('Alice', memory.task_prefix())
            profile = memory.profile_snapshot()
        from dataclasses import replace
        with SessionStore.create(replace(self.config(), root_dir=self.root / 'other-project')) as store:
            self.assertEqual(store.memory.profile_snapshot(), profile)
            self.assertEqual(store.memory.facts()[0]['object'], 'Alice')

    def test_nightly_profile_consolidation_uses_local_three_am(self):
        from datetime import datetime
        with SessionStore.create(self.config()) as store:
            self.assertTrue(store.memory.consolidate_due(None, datetime.fromisoformat('2026-01-02T02:59:00+08:00')))
            self.assertTrue(store.memory.consolidate_due(None, datetime.fromisoformat('2026-01-02T03:00:00+08:00')))
            self.assertFalse(store.memory.consolidate_due(None, datetime.fromisoformat('2026-01-02T03:01:00+08:00')))

    def test_automatic_batch_completion_refreshes_profile_without_noop_churn(self):
        from tests.test_memory_facts import StableModel
        with SessionStore.create(self.config(recent_task_count=1)) as store:
            store.record_task(1, 'I prefer concise responses')
            store.end_task()
            store.record_task(2, 'next')
            store.end_task()
            store.memory.process_pending(StableModel())
            profile = store.memory.profile_snapshot()
            self.assertIn('concise', profile['content'])
            store.memory.process_pending(StableModel())
            self.assertEqual(store.memory.profile_snapshot(), profile)
            fact = store.memory.facts()[0]
            from memory.stable_memory import timestamp
            corrected = store.memory.correct(fact['fact_id'], dict(subject='USER', predicate=fact['predicate'],
                object='detailed', category='communication'),
                source=dict(quote='Correction: detailed', recorded_at=timestamp()))
            self.assertIn('detailed', store.memory.task_prefix())
            store.memory.forget(corrected, source=dict(quote='Forget preference', recorded_at=timestamp()))
            self.assertNotIn('detailed', store.memory.task_prefix())
