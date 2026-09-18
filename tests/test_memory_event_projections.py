from contextlib import redirect_stdout
from io import StringIO
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from jarvis_agent import Agent
from memory_service import MemoryService

from tests.test_jarvis_agent import FakeClient, ScriptedClient
from model_client import ModelRequestError
from tests.test_session_persistence import SessionTestBase


class EventProjectionTests(SessionTestBase):
    def test_rebuild_during_request_keeps_active_and_completed_tasks_session_local(self):
        entered, release = Event(), Event()

        class ControlledModel:
            calls = 0

            def complete(self, *args):
                self.calls += 1
                if self.calls == 2:
                    entered.set()
                    if not release.wait(5):
                        raise ModelRequestError('test release timed out')
                return {'role': 'assistant', 'content': f'answer {self.calls}'}

        agent = Agent(self.config(recent_task_count=1), ControlledModel(), extraction_client=FakeClient([]))
        self.agents.append(agent)
        other = self.agent(FakeClient([{'role': 'assistant', 'content': 'other session answer'}]))
        with redirect_stdout(StringIO()):
            agent.run_request('completed input')
            other.run_request('other session input')
            worker = Thread(target=agent.run_request, args=('active input',))
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                agent.store.memory.rebuild_projections()
                recent = agent.store.recent_path.read_text(encoding='utf-8')
                self.assertIn('completed input', recent)
                self.assertIn('active input', recent)
                self.assertNotIn('other session input', recent)
                self.assertNotIn('active input', other.store.recent_path.read_text(encoding='utf-8'))
                self.assertEqual(agent.store.memory.pending_batches(), [])
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        agent.store.memory.rebuild_projections()
        recent = agent.store.recent_path.read_text(encoding='utf-8')
        self.assertIn('active input', recent)
        self.assertNotIn('completed input', recent)
        self.assertEqual(len(agent.store.memory.pending_batches()), 1)

    def test_migrated_projection_keeps_legacy_provenance_and_checkpoint(self):
        from session_store import session_directory
        fixtures = Path(__file__).parent / 'fixtures' / 'legacy'
        original = session_directory(self.config()) / 'legacy.jsonl'
        original.parent.mkdir(parents=True)
        original.write_bytes((fixtures / 'session.jsonl').read_bytes())
        trajectory = self.state / 'memory' / 'trajectories' / 'legacy.jsonl'
        trajectory.parent.mkdir(parents=True)
        trajectory_events = [json.loads(line) for line in
                             (fixtures / 'trajectory.jsonl').read_text(encoding='utf-8').splitlines()]
        for event in trajectory_events:
            if event['type'] == 'task':
                event['recent_task_count'] = 1
        trajectory.write_text(''.join(json.dumps(event) + '\n' for event in trajectory_events), encoding='utf-8')
        before = original.read_bytes(), trajectory.read_bytes()
        # Existing queue input from an un-migrated reader must keep resolving.
        memory = MemoryService(self.state / 'memory')
        self.addCleanup(memory.close)
        memory.rebuild_projections()
        agent = Agent(self.config(recent_task_count=1), FakeClient([]), resume='legacy',
                      extraction_client=FakeClient([]))
        self.agents.append(agent)
        canonical_path = agent.store.path
        canonical = agent.store.path.read_bytes()
        expected_messages = agent.messages
        batch, = agent.store.memory.pending_batches()
        self.assertEqual(batch['source_task_id'], 'legacy:tea')
        self.assertEqual(batch['trajectory_path'], str(trajectory))
        agent.store.memory.rebuild_projections()
        self.assertEqual(agent.messages, expected_messages)
        self.assertIn('[CONTEXT_COMPRESSED]', str(expected_messages))
        history = ''.join(path.read_text(encoding='utf-8')
                          for path in (memory.directory / 'history').glob('*.md'))
        self.assertEqual(history.count('"remember tea"'), 1)
        self.assertEqual(history.count('"rolled back input"'), 1)
        self.assertNotIn('conflicting answer', history)
        model = FakeClient([{'content': json.dumps({'candidates': [{
            'candidate_text': 'Tea preference', 'source_event_ids': ['tea-user'],
        }]})}])
        agent.close()
        memory.process_pending(model)
        self.assertEqual(memory.pending_candidates()[0]['sources'][0]['source_event_id'], 'tea-user')
        self.assertEqual((original.read_bytes(), trajectory.read_bytes()), before)
        self.assertEqual(canonical_path.read_bytes(), canonical)

    def test_failed_input_is_history_but_never_a_recent_slot_or_pending_batch(self):
        client = ScriptedClient([{'role': 'assistant', 'content': 'accepted answer'},
                                 ModelRequestError('failed model'),
                                 {'role': 'assistant', 'content': 'next answer'}])
        agent = Agent(self.config(recent_task_count=1), client, extraction_client=FakeClient([]))
        self.agents.append(agent)
        with redirect_stdout(StringIO()):
            agent.run_request('accepted input')
            self.assertIsNone(agent.run_request('failed input'))
            recent = agent.store.recent_path.read_text(encoding='utf-8')
            self.assertIn('accepted answer', recent)
            self.assertNotIn('failed input', recent)
            self.assertEqual(agent.store.memory.pending_batches(), [])
            agent.run_request('next input')
        events = self.records(agent.store.path)
        first = next(event for event in events if event['type'] == 'task')
        self.assertEqual([batch['source_task_id'] for batch in agent.store.memory.pending_batches()],
                         [first['task_id']])
        history = ''.join(path.read_text(encoding='utf-8')
                          for path in (agent.store.memory_directory / 'history').glob('*.md'))
        self.assertIn('failed input', history)
        self.assertNotIn('accepted answer', history)
        agent.store.memory.rebuild_projections()
        self.assertEqual(agent.store.recent_path.read_text(encoding='utf-8').count('## '), 1)

    def test_process_exit_after_terminal_commit_recovers_queue_and_torn_tail(self):
        script = '''
import json, os, sys
from pathlib import Path
from jarvis_agent import Agent, Config
class Client:
    def complete(self, *args):
        return {'role': 'assistant', 'content': 'answer'}
class Extractor:
    def complete(self, *args):
        raise RuntimeError('controlled offline extraction')
agent = Agent(Config(base_url='http://example.test', api_key='', model='test',
    root_dir=Path(sys.argv[1]), state_dir=Path(sys.argv[2]), recent_task_count=1),
    Client(), extraction_client=Extractor())
agent.run_request('first input')
sync = os.fsync
def commit(fd):
    sync(fd)
    events = [json.loads(line) for line in agent.store.path.read_text(encoding='utf-8').splitlines()]
    if events[-1]['type'] == 'task_end':
        os._exit(41)
os.fsync = commit
agent.run_request('second input')
'''
        process = subprocess.run([sys.executable, '-c', script, str(self.root), str(self.state)],
                                 capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 41, process.stderr.decode(errors='replace'))
        from session_store import SessionStore
        path = SessionStore.list_sessions(self.config())[0].path
        with path.open('ab') as handle:
            handle.write(b'{"type":"message"')
        original = path.read_bytes()
        directory = self.state / 'memory'
        for projection in (directory / 'history').glob('*.md'):
            projection.unlink()
        memory = MemoryService(directory)
        self.addCleanup(memory.close)
        memory.rebuild_projections()
        batch, = memory.pending_batches()
        model = FakeClient([{'content': '{"candidates": []}'}])
        replace = Path.replace

        def fail_pending_publication(path, target):
            if Path(target) == memory.pending_path:
                raise OSError('Pending publication failed after extraction commit')
            return replace(path, target)

        with patch.object(Path, 'replace', fail_pending_publication):
            with self.assertRaises(OSError):
                memory.process_pending(model)
        self.assertEqual(memory.pending_batches()[0]['status'], 'extracted')
        memory.close()
        memory = MemoryService(directory)
        self.addCleanup(memory.close)
        supplied = json.loads(model.requests[0][0][-1]['content'])
        self.assertEqual({event['task_id'] for event in supplied}, {batch['source_task_id']})
        self.assertIn('first input', str(supplied))
        self.assertNotIn('second input', str(supplied))
        memory.rebuild_projections()
        memory.process_pending(model)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(path.read_bytes(), original)

    def test_unavailable_recent_projection_does_not_block_committed_requests(self):
        agent = Agent(self.config(recent_task_count=1),
                      FakeClient([{'role': 'assistant', 'content': 'ok'}] * 3),
                      extraction_client=FakeClient([]))
        self.agents.append(agent)
        replace = Path.replace

        def unavailable(path, target):
            if Path(target) == agent.store.recent_path:
                raise OSError('Recent publication unavailable')
            return replace(path, target)

        with patch.object(Path, 'replace', unavailable), redirect_stdout(StringIO()):
            for query in ('first', 'second', 'third'):
                self.assertEqual(agent.run_request(query), 'ok')
        agent.store.memory.rebuild_projections()
        recent = agent.store.recent_path.read_text(encoding='utf-8')
        self.assertIn('third', recent)
        self.assertNotIn('second', recent)
        self.assertEqual(len(agent.store.memory.pending_batches()), 2)

    def test_recent_context_selects_original_messages_without_copying_evidence(self):
        client = FakeClient([{'role': 'assistant', 'content': answer}
                             for answer in ('first answer', 'second answer', 'third answer')])
        agent = Agent(self.config(recent_task_count=1), client, extraction_client=FakeClient([]))
        self.agents.append(agent)
        with redirect_stdout(StringIO()):
            for query in ('first input', 'second input', 'third input'):
                agent.run_request(query)
        self.assertNotIn('first answer', str(client.requests[2]))
        self.assertIn('second answer', str(client.requests[2]))
        records = self.records(agent.store.path)
        selections = [event for event in records if event['type'] == 'recent_context']
        self.assertTrue(selections)
        self.assertTrue(all('messages' not in event for event in selections))
        messages = {event['event_id']: event['message'] for event in records if event['type'] == 'message'}
        for selection in selections:
            self.assertTrue(selection['message_event_ids'])
            self.assertTrue(set(selection['message_event_ids']) <= messages.keys())
        expected = agent.messages
        session_id = agent.store.session_id
        agent.close()
        resumed = self.agent(FakeClient([]), resume=session_id, recent_task_count=1)
        self.assertEqual(resumed.messages, expected)
        self.assertFalse((resumed.store.memory_directory / 'trajectories').exists())

    def test_recovery_finds_eviction_after_queue_failure_without_resuming_session(self):
        agent = Agent(self.config(recent_task_count=1),
                      FakeClient([{'role': 'assistant', 'content': 'ok'}] * 2),
                      extraction_client=FakeClient([]))
        self.agents.append(agent)
        with redirect_stdout(StringIO()):
            agent.run_request('I prefer short answers')
            with sqlite3.connect(agent.store.memory.path) as db:
                db.execute("""CREATE TRIGGER unavailable_queue BEFORE INSERT ON pending_batches
                    BEGIN SELECT RAISE(ABORT, 'queue unavailable'); END""")
            self.assertEqual(agent.run_request('next input'), 'ok')
        self.assertEqual(agent.store.memory.pending_batches(), [])
        original = agent.store.path.read_bytes()
        path = agent.store.path
        directory = agent.store.memory_directory
        agent.close()
        with sqlite3.connect(directory / 'memory.db') as db:
            db.execute('DROP TRIGGER unavailable_queue')
        memory = MemoryService(directory)
        self.addCleanup(memory.close)
        memory.rebuild_projections()
        batch, = memory.pending_batches()
        events = self.records(path)
        task = next(event for event in events if event['type'] == 'task')
        self.assertEqual(batch['source_task_id'], task['task_id'])
        self.assertEqual(batch['trajectory_path'], str(path))
        model = FakeClient([{'content': json.dumps({'candidates': [{
            'candidate_text': 'Prefers short answers', 'source_event_ids': [task['event_id']],
        }]})}])
        memory.process_pending(model)
        source, = memory.pending_candidates()[0]['sources']
        self.assertEqual(source['source_event_id'], task['event_id'])
        for _ in range(2):
            memory.rebuild_projections()
            memory.process_pending(model)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(len(memory.pending_batches()), 1)
        self.assertEqual(path.read_bytes(), original)

    def test_resume_rebuilds_edited_and_deleted_history_without_changing_evidence(self):
        first = self.agent(FakeClient([{'role': 'assistant', 'content': 'answer A'}]))
        second = self.agent(FakeClient([{'role': 'assistant', 'content': 'answer B'}]))
        with redirect_stdout(StringIO()):
            first.run_request('input A')
            second.run_request('input B')
        history = next((first.store.memory_directory / 'history').glob('*.md'))
        expected = history.read_text(encoding='utf-8')
        original = first.store.path.read_bytes()
        session_id = first.store.session_id
        recent = first.store.recent_path
        first.close()
        second.close()
        for damage in ('edit', 'delete'):
            with self.subTest(damage=damage):
                if damage == 'edit':
                    history.write_text('fabricated user history', encoding='utf-8')
                    recent.write_text('fabricated conversation', encoding='utf-8')
                else:
                    history.unlink()
                    recent.unlink()
                resumed = self.agent(FakeClient([]), resume=session_id)
                self.assertEqual(history.read_text(encoding='utf-8'), expected)
                self.assertIn('input A', recent.read_text(encoding='utf-8'))
                self.assertNotIn('fabricated', str(resumed.messages))
                self.assertEqual(resumed.store.path.read_bytes(), original)
                self.assertNotIn('answer A', expected)
                self.assertNotIn('answer B', expected)
                resumed.close()
