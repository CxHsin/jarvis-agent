import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from memory_service import MemoryService
from application import assemble_memory

from session_store import SessionStore, session_directory
from tests.test_jarvis_agent import FakeClient
from tests.test_session_persistence import SessionTestBase


CRASH_SCRIPT = '''
import os, sys
from pathlib import Path
from application import assemble_memory
from configuration import Config
from session_store import SessionStore
config = Config(base_url='http://example.test', api_key='', model='test', root_dir=Path(sys.argv[1]), state_dir=Path(sys.argv[2]))
memory = assemble_memory(config) if sys.argv[4] == 'session' else None
sync, replace = os.fsync, os.replace
count = 0
def fsync(fd):
    global count
    sync(fd)
    count += 1
    if sys.argv[3] == 'sync-' + str(count): os._exit(27)
def rename(source, target):
    replace(source, target)
    if sys.argv[3] == Path(target).name: os._exit(27)
os.fsync, os.replace = fsync, rename
if sys.argv[4] == 'session':
    SessionStore.resume(config, 'legacy', memory=memory)
else:
    assemble_memory(config)
'''

class LegacyMigrationTests(SessionTestBase):
    def representative(self):
        original = self.legacy()
        fixtures = Path(__file__).parent / 'fixtures' / 'legacy'
        original.write_bytes((fixtures / 'session.jsonl').read_bytes() + b'{"type":"message"')
        trajectory = self.state / 'memory' / 'trajectories' / 'legacy.jsonl'
        trajectory.parent.mkdir(parents=True)
        trajectory.write_bytes((fixtures / 'trajectory.jsonl').read_bytes())
        return original, trajectory

    def test_compressed_rollback_and_conflicting_evidence_survive_with_sources(self):
        original, trajectory = self.representative()
        before, evidence = original.read_bytes(), trajectory.read_bytes()
        agent = self.agent(FakeClient([]), resume='legacy')
        self.assertEqual([message['content'] for message in agent.messages[1:]],
                         ['[CONTEXT_COMPRESSED] tea summary', 'continue', 'continued'])
        events = self.records(agent.store.path)
        self.assertIn('rolled back input', str(events))
        self.assertIn('conflicting answer', str(events))
        self.assertNotIn('rolled back input', str(agent.store.recent_messages()))
        self.assertTrue(any(event['event_id'] == 'tea-user' for event in events))
        report = json.loads((agent.store.path.parent / 'committed.json').read_text(encoding='utf-8'))
        self.assertTrue(report['conflicts'])
        self.assertTrue(report['warnings'])
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(trajectory.read_bytes(), evidence)

    def test_interruption_at_each_durable_boundary_retries_without_duplicate_import(self):
        original, trajectory = self.representative()

        # Each run starts from the same fixed legacy pair in a fresh state root.
        import shutil
        for boundary in ['sync-1', 'sync-2', 'sync-3', 'backup', 'sync-4', 'events.jsonl',
                         'sync-5', 'committed.json']:
            with self.subTest(boundary=boundary):
                state = self.state.parent / boundary
                shutil.copytree(self.state, state)
                old_state = self.state
                self.state = state
                try:
                    process = subprocess.run([sys.executable, '-c', CRASH_SCRIPT, str(self.root), str(state), boundary, 'session'],
                                             capture_output=True, timeout=30)
                    self.assertEqual(process.returncode, 27, process.stderr.decode(errors='replace'))
                    agent = self.agent(FakeClient([]), resume='legacy')
                    events = self.records(agent.store.path)
                    self.assertEqual(len(events), 24)
                    self.assertEqual(len({event['event_id'] for event in events}), 24)
                    self.assertEqual((agent.store.path.parent / 'backup' / 'session.jsonl').read_bytes(), original.read_bytes())
                    self.assertEqual((agent.store.path.parent / 'backup' / 'trajectory.jsonl').read_bytes(), trajectory.read_bytes())
                    agent.close()
                    again = self.agent(FakeClient([]), resume='legacy')
                    self.assertEqual(self.records(again.store.path), events)
                    again.close()
                finally:
                    self.state = old_state

    def test_validation_failure_keeps_old_data_readable_and_does_not_commit(self):
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

    def test_memory_identity_correction_forgetting_profile_and_provenance_are_preserved(self):
        original, trajectory = self.representative()
        memory = MemoryService(self.state / 'memory')
        source = dict(quote='remember tea', recorded_at='2026-01-01T00:00:01+00:00',
                      source_task_id='legacy:tea', source_event_id='tea-user', trajectory_path=str(trajectory))
        fact = dict(subject='USER', predicate='likes', object='tea', text='tea', category='work_preferences')
        first = memory.remember(fact, source=source, operation_id='remember-tea')
        correction = dict(source, quote='prefer green tea', source_event_id='manual-correction',
                          trajectory_path='', recorded_at='2026-01-02T00:00:00+00:00')
        second = memory.correct(first, dict(fact, object='green tea', text='green tea'),
                                source=correction, operation_id='correct-tea')
        coffee = memory.remember(dict(fact, object='coffee', text='coffee'),
                                 source=dict(correction, quote='coffee'), operation_id='remember-coffee')
        memory.forget(coffee, source=dict(correction, quote='forget coffee'), operation_id='forget-coffee')
        before = memory.facts(include_inactive=True)
        profiles = memory.profile_versions()
        query = memory.search('tea')['facts']
        store = SessionStore.resume(self.config(), 'legacy', memory=memory)
        try:
            self.assertEqual(memory.facts(include_inactive=True), before)
            self.assertEqual(memory.profile_versions(), profiles)
            self.assertEqual(memory.search('tea')['facts'], query)
            self.assertEqual(memory.search('coffee', include_history=True)['facts'], [])
            mapping = json.loads((store.path.parent / 'committed.json').read_text(encoding='utf-8'))['sources']
            self.assertTrue(any(item['source']['event_id'] == 'tea-user' and item['event_id'] == 'tea-user'
                                for item in mapping))
            import shutil
            copied_backup = self.state.parent / 'inspect-backup'
            shutil.copytree(store.path.parent / 'backup', copied_backup)
            backup_memory = MemoryService(copied_backup)
            try:
                self.assertEqual(backup_memory.facts(include_inactive=True), before)
                self.assertEqual(backup_memory.profile_versions(), profiles)
            finally:
                backup_memory.close()
            self.assertNotEqual(first, second)
        finally:
            store.close()
            memory.close()

    def test_existing_wal_database_and_manual_profile_are_backed_up_before_startup(self):
        self.legacy()
        import sqlite3
        directory = self.state / 'memory'
        memory = MemoryService(directory)
        before = memory.profile_versions()
        memory.close()
        manual = b'# User profile\n\nDo not overwrite this unimported edit.\n'
        (directory / 'memory.md').write_bytes(manual)
        database = sqlite3.connect(directory / 'memory.db')
        try:
            database.execute('PRAGMA journal_mode=WAL')
            database.execute('CREATE TABLE migration_wal_evidence (value TEXT)')
            database.execute("INSERT INTO migration_wal_evidence VALUES ('committed WAL value')")
            database.commit()
            from dataclasses import replace
            other_workspace = self.root.parent / 'new-workspace'
            other_workspace.mkdir()
            prepared = assemble_memory(replace(self.config(), root_dir=other_workspace))
            prepared.close()
            backup = self.state / 'migrations' / 'legacy-memory'
            self.assertEqual((backup / 'memory.md').read_bytes(), manual)
            from contextlib import closing
            with closing(sqlite3.connect(backup / 'memory.db')) as copied:
                self.assertEqual(copied.execute('SELECT value FROM migration_wal_evidence').fetchone()[0],
                                 'committed WAL value')
                self.assertEqual(copied.execute('SELECT COUNT(*) FROM profile_versions').fetchone()[0], len(before))
            snapshot = (backup / 'memory.db').read_bytes()
            prepared = assemble_memory(self.config())
            prepared.close()
            self.assertEqual((backup / 'memory.db').read_bytes(), snapshot)
        finally:
            database.close()

    def test_post_switch_projection_failure_reports_durable_commit_and_retries(self):
        original = self.legacy()
        recent_parent = self.state / 'memory' / 'recent' / 'legacy'
        recent_parent.parent.mkdir(parents=True)
        recent_parent.write_text('blocked derived directory', encoding='utf-8')
        from session_store import SessionError
        with self.assertRaisesRegex(SessionError, '迁移已经提交'):
            self.agent(FakeClient([]), resume='legacy')
        canonical = original.parent / 'migrations' / 'legacy' / 'events.jsonl'
        committed = canonical.read_bytes()
        recent_parent.unlink()
        agent = self.agent(FakeClient([]), resume='legacy')
        self.assertEqual(agent.messages[-1]['content'], 'old answer')
        self.assertEqual(canonical.read_bytes(), committed)

    def test_initial_memory_backup_retries_at_each_publication_boundary(self):
        self.legacy()
        memory = MemoryService(self.state / 'memory')
        memory.close()

        import shutil
        for boundary in ['sync-1', 'sync-2', 'sync-3', 'legacy-memory']:
            with self.subTest(boundary=boundary):
                state = self.state.parent / ('initial-' + boundary)
                shutil.copytree(self.state, state)
                old_state = self.state
                self.state = state
                try:
                    process = subprocess.run([sys.executable, '-c', CRASH_SCRIPT, str(self.root), str(state), boundary, 'memory'],
                                             capture_output=True, timeout=30)
                    self.assertEqual(process.returncode, 27, process.stderr.decode(errors='replace'))
                    prepared = assemble_memory(self.config())
                    prepared.close()
                    backup = state / 'migrations' / 'legacy-memory'
                    self.assertTrue((backup / 'memory.db').is_file())
                    self.assertEqual((backup / 'memory.md').read_bytes(),
                                     (old_state / 'memory' / 'memory.md').read_bytes())
                finally:
                    self.state = old_state

    def test_torn_utf8_tail_is_preserved_but_interior_corruption_blocks_switch(self):
        original = self.legacy()
        valid = original.read_bytes()
        torn = valid + b'{"type":"message","message":{"content":"\xe4\xb8'
        original.write_bytes(torn)
        agent = self.agent(FakeClient([]), resume='legacy')
        self.assertEqual(agent.messages[-1]['content'], 'old answer')
        self.assertEqual((agent.store.path.parent / 'backup' / 'session.jsonl').read_bytes(), torn)
        self.assertTrue(agent.store.load().warnings)
        agent.close()

        # A separate source has corruption inside a complete line, not a torn tail.
        other = original.with_name('corrupt.jsonl')
        other.write_bytes(valid.replace(b'"legacy"', b'"corrupt"') + b'{"type":"message","content":"\xe4\xb8"}\n')
        with self.assertRaises(ValueError):
            self.agent(FakeClient([]), resume='corrupt')
        self.assertFalse((other.parent / 'migrations' / 'corrupt' / 'committed.json').exists())
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

    def test_first_resume_migrates_without_touching_original_and_reuses_events(self):
        original = self.legacy()
        before = original.read_bytes()
        agent = self.agent(FakeClient([]), resume='legacy')
        self.assertEqual(agent.messages[-1]['content'], 'old answer')
        events = self.records(agent.store.path)
        self.assertEqual(events[0]['version'], 2)
        self.assertTrue(all(event['event_id'] for event in events))
        from datetime import datetime, timezone
        self.assertTrue(all(datetime.fromisoformat(event['recorded_at']).utcoffset() == timezone.utc.utcoffset(None)
                            for event in events))
        self.assertEqual(original.read_bytes(), before)
        canonical = agent.store.path
        agent.close()
        again = self.agent(FakeClient([]), resume='legacy')
        self.assertEqual(again.store.path, canonical)
        self.assertEqual(self.records(canonical), events)
