"""Legacy sessions still migrate without reviving the removed memory subsystem."""
import json
from pathlib import Path
from unittest.mock import patch

from session.session_store import SessionStore, session_directory
from tests.test_jarvis_agent import FakeClient
from tests.test_session_persistence import SessionTestBase


class LegacyMigrationTests(SessionTestBase):
    def legacy(self):
        path = session_directory(self.config()) / 'legacy.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text('\n'.join(json.dumps(event) for event in [
            dict(type='session', version=1, id='legacy', workspace=str(self.root), started_at='2026-01-01'),
            dict(type='task', number=1, goal='old question'),
            dict(type='message', message=dict(role='user', content='old question')),
            dict(type='message', message=dict(role='assistant', content='old answer')),
        ]) + '\n', encoding='utf-8')
        return path

    def test_first_resume_migrates_without_touching_original(self):
        original = self.legacy()
        before = original.read_bytes()
        agent = self.agent(FakeClient([]), resume='legacy')
        self.assertEqual(agent.messages[-1]['content'], 'old answer')
        canonical = agent.store.path
        self.assertEqual(self.records(canonical)[0]['version'], 2)
        self.assertEqual(original.read_bytes(), before)
        agent.close()
        again = self.agent(FakeClient([]), resume='legacy')
        self.assertEqual(again.store.path, canonical)

    def representative(self):
        original = self.legacy()
        fixtures = Path(__file__).parent / 'fixtures' / 'legacy'
        original.write_bytes((fixtures / 'session.jsonl').read_bytes() + b'{"type":"message"')
        trajectory = self.state / 'memory' / 'trajectories' / 'legacy.jsonl'
        trajectory.parent.mkdir(parents=True)
        trajectory.write_bytes((fixtures / 'trajectory.jsonl').read_bytes())
        return original, trajectory

    def test_old_events_and_trajectory_survive_migration(self):
        original, trajectory = self.representative()
        before, evidence = original.read_bytes(), trajectory.read_bytes()
        agent = self.agent(FakeClient([]), resume='legacy')
        self.assertEqual([message['content'] for message in agent.messages[1:]],
                         ['[CONTEXT_COMPRESSED] tea summary', 'continue', 'continued'])
        events = self.records(agent.store.path)
        self.assertIn('rolled back input', str(events))
        self.assertNotIn('rolled back input', str(agent.store.recent_messages()))
        report = json.loads((agent.store.path.parent / 'committed.json').read_text(encoding='utf-8'))
        self.assertTrue(report['conflicts'])
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(trajectory.read_bytes(), evidence)
        self.assertFalse((agent.store.path.parent / 'backup' / 'memory.db').exists())

    def test_validation_failure_never_switches_canonical_log(self):
        original = self.legacy()
        before = original.read_bytes()
        import os
        replace = os.replace
        def corrupt_candidate(source, target):
            replace(source, target)
            if Path(target).name == 'events.jsonl':
                Path(target).write_text('{}\n', encoding='utf-8')
        with patch('os.replace', side_effect=corrupt_candidate):
            with self.assertRaises(ValueError):
                self.agent(FakeClient([]), resume='legacy')
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(SessionStore.list_sessions(self.config())[0].last_user_text, 'old question')
        self.assertFalse((original.parent / 'migrations' / 'legacy' / 'committed.json').exists())
        agent = self.agent(FakeClient([]), resume='legacy')
        self.assertEqual(agent.messages[-1]['content'], 'old answer')
