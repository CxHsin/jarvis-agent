from contextlib import redirect_stdout
from io import StringIO

from tests.test_session_persistence import SessionTestBase
from tests.test_jarvis_agent import FakeClient
from session.session_store import SessionStore


class HistoryRecentTests(SessionTestBase):
    def test_resume_after_partial_trajectory_record_keeps_later_rounds(self):
        store = SessionStore.create(self.config())
        store.record_task(1, "first")
        store.record_message({"role": "user", "content": "first"})
        session_id, path = store.session_id, store.history.path
        store.close()
        with path.open("ab") as handle:
            handle.write(b'{"type": "message"')
        with SessionStore.resume(self.config(), session_id) as resumed:
            resumed.record_message({"role": "assistant", "content": "after interruption"})
        with SessionStore.resume(self.config(), session_id) as resumed:
            self.assertIn("after interruption", str(resumed.recent_messages()))

    def test_active_rounds_and_original_evidence_survive_compaction(self):
        with SessionStore.create(self.config(recent_task_count=1)) as store:
            store.record_task(1, "find evidence")
            store.record_message({"role": "user", "content": "find evidence"})
            store.record_message({"role": "assistant", "tool_calls": [
                {"id": "call", "type": "function", "function": {"name": "read", "arguments": "{}"}}
            ]})
            store.record_message({"role": "tool", "tool_call_id": "call", "content": "immutable original"})
            self.assertIn("immutable original", store.recent_path.read_text(encoding="utf-8"))
            store.record_compact(4, "short checkpoint", "model", ["call"])
            self.assertNotIn("immutable original", str(store.load().messages))
            self.assertIn("immutable original", str(store.recent_messages()))
            store.end_task()
            store.record_task(2, "active query")
            store.record_message({"role": "user", "content": "active query"})
            recent = store.recent_path.read_text(encoding="utf-8")
            self.assertIn("immutable original", recent)
            self.assertIn("active query", recent)
            evidence = store.history.path.read_text(encoding="utf-8")
            self.assertIn("immutable original", evidence)
            self.assertIn("short checkpoint", evidence)

    def test_recent_window_and_history_survive_resume(self):
        agent = self.agent(FakeClient([
            {"role": "assistant", "content": "answer one"},
            {"role": "assistant", "content": "answer two"},
        ]), recent_task_count=1)
        with redirect_stdout(StringIO()):
            agent.run_request("query one")
            agent.run_request("query two")
        directory = agent.store.memory_directory
        history = next((directory / "history").glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("query one", history)
        self.assertIn("query two", history)
        self.assertNotIn("answer one", history)
        self.assertIn("occurred_at", history)
        self.assertIn("recorded_at", history)
        recent_path = agent.store.recent_path
        recent = recent_path.read_text(encoding="utf-8")
        self.assertNotIn("query one", recent)
        self.assertIn("answer two", recent)
        session_id = agent.store.session_id
        agent.close()
        client = FakeClient([{"role": "assistant", "content": "answer three"}])
        resumed = self.agent(client, resume=session_id, recent_task_count=1)
        self.assertEqual(recent_path.read_text(encoding="utf-8"), recent)
        with redirect_stdout(StringIO()):
            resumed.run_request("query three")
        sent = str(client.requests[0])
        self.assertIn("answer two", sent)
        self.assertNotIn("query one", sent)
