from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import subprocess
import sys
from pathlib import Path

from session_store import SessionStore, session_directory
from tests.test_jarvis_agent import FakeClient, FailingClient
from tests.test_session_persistence import SessionTestBase


class UnifiedSessionEventsTests(SessionTestBase):
    def test_exit_after_context_reset_does_not_reintroduce_failed_input(self):
        agent = self.agent(FailingClient())
        with redirect_stdout(StringIO()):
            agent.run_request("failed input")
        path, session_id = agent.store.path, agent.store.session_id
        agent.close()
        # A crash may leave the committed reset but no final task status.
        records = self.records(path)
        self.assertEqual(records[-1]["type"], "task_end")
        path.write_text("\n".join(json.dumps(record) for record in records[:-1]) + "\n", encoding="utf-8")
        client = FakeClient([{"role": "assistant", "content": "next answer"}])
        resumed = self.agent(client, resume=session_id)
        with redirect_stdout(StringIO()):
            resumed.run_request("next input")
        self.assertNotIn("failed input", str(client.requests))

    def test_missing_tool_result_is_unknown_and_repair_survives_second_resume(self):
        with SessionStore.create(self.config()) as store:
            store.record_task(1, "interrupted edit")
            store.record_message({"role": "user", "content": "write result"})
            store.record_message({"role": "assistant", "content": None, "tool_calls": [
                {"id": "unknown-edit", "function": {"name": "edit", "arguments":
                    '{"path":"must-not-exist.txt","content":"do not replay"}'}}
            ]})
            store.record_tool_audit({"phase": "started", "call_id": "unknown-edit", "tool_id": "edit"})
            path, session_id = store.path, store.session_id
        with path.open("ab") as handle:
            handle.write(b'{"type":"message","message":')
        original = path.read_bytes()
        resumed = self.agent(FakeClient([]), resume=session_id)
        repair = resumed.messages[-1]
        self.assertEqual(repair["tool_call_id"], "unknown-edit")
        self.assertIn("执行状态未知", repair["content"])
        self.assertFalse((self.root / "must-not-exist.txt").exists())
        resumed.close()
        repaired_bytes = path.read_bytes()
        self.assertTrue(repaired_bytes.startswith(original))
        again = self.agent(FakeClient([]), resume=session_id)
        self.assertEqual(again.messages[-1], repair)
        self.assertEqual(path.read_bytes(), repaired_bytes)

    def test_process_exit_and_partial_tail_resume_without_reexecuting_edit(self):
        script = '''
import os
from pathlib import Path
from jarvis_agent import Agent, Config
class Client:
    calls = 0
    def complete(self, messages, tools, tool_choice):
        self.calls += 1
        if self.calls == 1:
            return {"role": "assistant", "content": None, "tool_calls": [
                {"id": "edit-1", "function": {"name": "edit", "arguments":
                    '{"path":"result.txt","content":"written once"}'}}
            ]}
        os._exit(23)
agent = Agent(Config(base_url="http://example.test", api_key="", model="test",
    root_dir=Path(__import__("sys").argv[1]), state_dir=Path(__import__("sys").argv[2]),
    tool_permission_mode="broad-access"), Client())
agent.run_request("write result")
'''
        process = subprocess.run([sys.executable, "-c", script, str(self.root), str(self.state)],
                                 cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 23, process.stderr.decode(errors="replace"))
        result = self.root / "result.txt"
        self.assertEqual(result.read_text(encoding="utf-8"), "written once")
        result.write_text("changed after exit", encoding="utf-8")
        info = SessionStore.list_sessions(self.config())[0]
        with info.path.open("ab") as handle:
            handle.write(b'{"type":"message","message":')
        original = info.path.read_bytes()
        client = FakeClient([{"role": "assistant", "content": "resumed"}])
        resumed = self.agent(client, resume=info.id)
        self.assertTrue(any(message.get("tool_call_id") == "edit-1" for message in resumed.messages))
        with redirect_stdout(StringIO()):
            self.assertEqual(resumed.run_request("continue"), "resumed")
        resumed.close()
        self.assertEqual(result.read_text(encoding="utf-8"), "changed after exit")
        self.assertTrue(info.path.read_bytes().startswith(original))
        again = self.agent(FakeClient([]), resume=info.id)
        self.assertEqual(again.messages[-1]["content"], "resumed")

    def test_legacy_session_remains_readable_and_keeps_original_bytes(self):
        session_id = "20260101-000000-abcd"
        path = session_directory(self.config()) / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True)
        records = [
            {"type": "session", "version": 1, "id": session_id,
             "workspace": str(self.root), "started_at": "2026-01-01T00:00:00"},
            {"type": "task", "number": 1, "goal": "old question"},
            {"type": "message", "message": {"role": "user", "content": "old question"}},
            {"type": "message", "message": {"role": "assistant", "content": "old answer"}},
        ]
        original = ("\n".join(json.dumps(record) for record in records) + "\n").encode()
        path.write_bytes(original)
        self.assertEqual(SessionStore.list_sessions(self.config())[0].last_user_text, "old question")
        resumed = self.agent(FakeClient([{"role": "assistant", "content": "new answer"}]), resume=session_id)
        self.assertEqual(resumed.messages[-1]["content"], "old answer")
        with redirect_stdout(StringIO()):
            self.assertEqual(resumed.run_request("new question"), "new answer")
        self.assertTrue(path.read_bytes().startswith(original))

    def test_failed_request_keeps_raw_events_but_restores_visible_context(self):
        agent = self.agent(FailingClient())
        path, session_id = agent.store.path, agent.store.session_id
        with redirect_stdout(StringIO()):
            self.assertIsNone(agent.run_request("failed input"))
        records = self.records(path)
        self.assertTrue(any(record.get("message", {}).get("content") == "failed input"
                            for record in records))
        self.assertEqual(records[-1]["status"], "failed")
        self.assertEqual([message["role"] for message in agent.messages], ["system"])
        agent.close()
        resumed = self.agent(FakeClient([{"role": "assistant", "content": "next answer"}]), resume=session_id)
        self.assertEqual([message["role"] for message in resumed.messages], ["system"])
        with redirect_stdout(StringIO()):
            resumed.run_request("next input")
        self.assertEqual(self.records(path)[:len(records)], records)
        tasks = [record for record in self.records(path) if record["type"] == "task"]
        self.assertNotEqual(tasks[0]["task_id"], tasks[1]["task_id"])

    def test_task_and_tool_evidence_share_durable_identity_after_resume(self):
        (self.root / "note.md").write_text("durable evidence\n", encoding="utf-8")
        agent = self.agent(FakeClient([
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "read-1", "function": {"name": "read_file", "arguments": '{"path":"note.md"}'}}
            ]},
            {"role": "assistant", "content": "found it"},
        ]))
        with redirect_stdout(StringIO()):
            self.assertEqual(agent.run_request("read the note"), "found it")
        expected = deepcopy(agent.messages)
        path, session_id = agent.store.path, agent.store.session_id
        records = self.records(path)
        self.assertTrue(all(record.get("event_version") == 1 for record in records))
        self.assertEqual([record["sequence"] for record in records], list(range(1, len(records) + 1)))
        self.assertEqual(len({record["event_id"] for record in records}), len(records))
        self.assertEqual({record["session_id"] for record in records}, {session_id})
        task = next(record for record in records if record["type"] == "task")
        task_records = records[records.index(task):]
        self.assertEqual({record["task_id"] for record in task_records}, {task["task_id"]})
        self.assertTrue({"message", "archive", "tool_audit", "task_end"}.issubset(
            {record["type"] for record in task_records}))
        history = next((agent.store.memory_directory / "history").glob("*.md")).read_text(encoding="utf-8")
        self.assertIn(task["event_id"], history)
        agent.close()
        resumed = self.agent(FakeClient([]), resume=session_id)
        self.assertEqual(resumed.messages, expected)
        self.assertEqual(self.records(path), records)
