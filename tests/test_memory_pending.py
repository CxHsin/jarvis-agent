from session.session_store import SessionStore
from tests.test_session_persistence import SessionTestBase
from tests.test_jarvis_agent import FakeClient, FailingClient
import json
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from dataclasses import replace
from io import StringIO
from threading import Event
import time
from agent.agent import Agent


class PendingTests(SessionTestBase):
    def test_eviction_persists_one_batch_and_resume_does_not_duplicate(self):
        with SessionStore.create(self.config(recent_task_count=1)) as store:
            store.record_task(1, "I prefer short answers")
            store.record_message({"role": "user", "content": "I prefer short answers"})
            store.end_task()
            store.record_task(2, "next")
            store.end_task()
            batches = store.memory.pending_batches()
            self.assertEqual(len(batches), 1)
            self.assertEqual(batches[0]["status"], "pending_extraction")
            self.assertTrue(batches[0]["source_task_id"])
            self.assertTrue(batches[0]["source_event_ids"])
            self.assertNotIn("I prefer short answers", store.memory.pending_path.read_text(encoding="utf-8"))
            session_id = store.session_id
        with SessionStore.resume(self.config(recent_task_count=1), session_id) as store:
            self.assertEqual(store.memory.pending_batches(), batches)

    def test_failed_extraction_retries_complete_evidence_after_restart(self):
        with SessionStore.create(self.config(recent_task_count=1)) as store:
            store.record_task(1, "I prefer short answers")
            store.record_message({"role": "user", "content": "I prefer short answers"})
            store.record_message({"role": "assistant", "content": "acknowledged"})
            store.record_message({"role": "tool", "content": "original evidence", "tool_call_id": "c1"})
            store.record_compact(4, "checkpoint", "model")
            store.end_task()
            store.record_task(2, "next")
            store.end_task()
            batch = store.memory.pending_batches()[0]
            store.memory.process_pending(FailingClient())
            self.assertEqual(store.memory.pending_batches()[0]["status"], "failed")
            self.assertNotIn("original evidence", str(store.recent_messages()))
            session_id = store.session_id
        with SessionStore.resume(self.config(recent_task_count=1), session_id) as store:
            model = FakeClient([{"role": "assistant", "content": json.dumps({"candidates": [{
                "candidate_text": "Prefers concise responses", "source_event_ids": [batch["source_event_ids"][0]],
                "occurred_at": None,
            }]})}])
            store.memory.process_pending(model)
            self.assertIn("original evidence", str(model.requests))
            candidate = store.memory.pending_candidates()[0]
            self.assertEqual(candidate["candidate_text"], "Prefers concise responses")
            self.assertEqual(candidate["sources"][0]["recorded_at"], batch["recorded_at"])
            self.assertEqual(candidate["sources"][0]["source_task_id"], batch["source_task_id"])
            self.assertIsNone(candidate["occurred_at"])
            self.assertIn("Prefers concise responses", store.memory.pending_path.read_text(encoding="utf-8"))
            self.assertNotIn("original evidence", store.memory.pending_path.read_text(encoding="utf-8"))
            store.memory.process_pending(model)
            self.assertEqual(len(store.memory.pending_candidates()), 1)

    def test_candidate_expires_thirty_days_after_evidence_not_extraction(self):
        with SessionStore.create(self.config(recent_task_count=1)) as store:
            store.record_task(1, "I prefer concise replies")
            store.end_task()
            store.record_task(2, "next")
            store.end_task()
            batch = store.memory.pending_batches()[0]
            model = FakeClient([{"content": json.dumps({"candidates": [{
                "candidate_text": "Prefers concise replies", "source_event_ids": [batch["source_event_ids"][0]],
            }]})}])
            store.memory.process_pending(model)
            received = datetime.fromisoformat(batch["recorded_at"])
            store.memory.expire_candidates(received + timedelta(days=30, microseconds=-1))
            self.assertEqual(store.memory.pending_candidates()[0]["status"], "pending")
            store.memory.expire_candidates(received + timedelta(days=30))
            candidate = store.memory.pending_candidates()[0]
            self.assertEqual(candidate["status"], "expired")
            self.assertIn("30 days", candidate["reason"])
            self.assertTrue(candidate["sources"])

    def test_agent_runs_real_extraction_adapter_off_foreground_and_excludes_pending(self):
        entered, release = Event(), Event()

        class ExternalModel:
            def complete(self, messages, tools, tool_choice):
                events = json.loads(messages[-1]["content"])
                entered.set()
                release.wait(5)
                return {"content": json.dumps({"candidates": [{
                    "candidate_text": "PENDING_SECRET", "source_event_ids": [events[0]["event_id"]],
                }]})}

        foreground = FakeClient([{"role": "assistant", "content": "ok"}] * 3)
        with redirect_stdout(StringIO()):
            agent = Agent(self.config(recent_task_count=1), foreground, extraction_client=ExternalModel())
            self.agents.append(agent)
            try:
                agent.run_request("I prefer concise answers")
                agent.run_request("second")
                self.assertTrue(entered.wait(2))
                self.assertEqual(agent.run_request("third"), "ok")
            finally:
                release.set()
            deadline = time.monotonic() + 3
            while not agent.store.memory.pending_candidates() and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(agent.store.memory.pending_candidates()[0]["candidate_text"], "PENDING_SECRET")
        self.assertNotIn("PENDING_SECRET", str(foreground.requests))

    def test_pending_is_shared_across_workspaces_and_new_evidence_extends_expiry(self):
        class ExternalModel:
            def complete(self, messages, tools, tool_choice):
                event = json.loads(messages[-1]["content"])[0]
                return {"content": json.dumps({"candidates": [{
                    "candidate_text": "Prefers concise replies", "source_event_ids": [event["event_id"]],
                }]})}

        with SessionStore.create(self.config(recent_task_count=1)) as first:
            first.record_task(1, "I prefer concise replies")
            first.end_task()
            first.record_task(2, "next")
            first.end_task()
            first.memory.process_pending(ExternalModel())
            original = first.memory.pending_candidates()[0]
            another_root = self.root.parent / "another-workspace"
            another_root.mkdir()
            with SessionStore.create(replace(self.config(recent_task_count=1), root_dir=another_root)) as second:
                self.assertEqual(second.memory.pending_candidates(), [original])
                second.record_task(1, "I still prefer concise replies")
                second.end_task()
                second.record_task(2, "next")
                second.end_task()
                second.memory.process_pending(ExternalModel())
                second.memory.expire_candidates(datetime.fromisoformat(original["last_evidence_at"]) + timedelta(days=30))
                updated = second.memory.pending_candidates()[0]
                self.assertEqual(updated["status"], "pending")
                self.assertEqual(len(updated["sources"]), 2)
                self.assertGreater(updated["last_evidence_at"], original["last_evidence_at"])

    def test_shutdown_during_extraction_leaves_batch_recoverable(self):
        from application import Application
        from threading import Thread

        entered, release, shutdown_finished = Event(), Event(), Event()
        observations = []

        class SlowExternalModel:
            def complete(self, messages, tools, tool_choice):
                entered.set()
                release.wait(5)
                return {"content": '{"candidates": []}'}

            def close(self):
                observations.append(release.is_set())

        app = Application(self.config(recent_task_count=1),
                          client=FakeClient([{"content": "ok"}] * 2),
                          extraction_client=SlowExternalModel())
        agent = app.create_session()
        memory = agent.store.memory
        session_id = agent.store.session_id
        agent.run_request("preference")
        agent.run_request("next")
        self.assertTrue(entered.wait(2))

        def shutdown():
            app.close()
            shutdown_finished.set()

        worker = Thread(target=shutdown)
        worker.start()
        try:
            deadline = time.monotonic() + 2
            while memory.pending_batches()[0]['status'] != 'failed' and time.monotonic() < deadline:
                shutdown_finished.wait(0.01)
            self.assertEqual(memory.pending_batches()[0]['status'], 'failed')
            self.assertFalse(shutdown_finished.wait(0.3))
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(observations, [True])
            with SessionStore.resume(self.config(recent_task_count=1), session_id) as resumed:
                resumed.memory.process_pending(FakeClient([{"content": '{"candidates": []}'}]))
                self.assertEqual(resumed.memory.pending_batches()[0]["status"], "extracted")
        finally:
            release.set()
            worker.join(5)
            app.close()
