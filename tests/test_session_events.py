from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from session.session_store import SessionStore, session_directory
from tests.test_jarvis_agent import FakeClient, FailingClient, ScriptedClient
from models.model_client import ModelRequestError
from tests.test_session_persistence import SessionTestBase


class UnifiedSessionEventsTests(SessionTestBase):
    def test_committed_message_and_rollback_survive_derived_projection_failure(self):
        for failure_kind in ("message", "context_rollback"):
            with self.subTest(failure_kind=failure_kind):
                client = FailingClient() if failure_kind == "context_rollback" else FakeClient([
                    {"role": "assistant", "content": "committed answer"}])
                agent = self.agent(client)
                original_record = agent.store.history.record
                def fail_projection(record):
                    if record["type"] == failure_kind:
                        raise OSError("projection unavailable")
                    return original_record(record)
                with patch.object(agent.store.history, "record", side_effect=fail_projection):
                    with redirect_stdout(StringIO()):
                        answer = agent.run_request("recorded input")
                self.assertEqual(answer, None if failure_kind == "context_rollback" else "committed answer")
                self.assertEqual(agent.messages[1:], agent.store.load().messages)
                expected = deepcopy(agent.messages)
                session_id = agent.store.session_id
                agent.close()
                resumed = self.agent(FakeClient([]), resume=session_id)
                self.assertEqual(resumed.messages, expected)

    def test_committed_compaction_survives_derived_projection_failure(self):
        agent = self.agent(FakeClient([{"role": "assistant", "content": "old answer"},
                                       {"role": "assistant", "content": "new answer"}]))
        with redirect_stdout(StringIO()):
            agent.run_request("old question")
            agent.run_request("new question")
        session_id = agent.store.session_id
        # Inject after the canonical event commit, at the derived-view boundary.
        with patch.object(agent.store.history, "record", side_effect=OSError("projection unavailable")):
            with redirect_stdout(StringIO()):
                self.assertTrue(agent.compact_now().compacted)
        expected = deepcopy(agent.messages)
        agent.close()
        resumed = self.agent(FakeClient([]), resume=session_id)
        self.assertEqual(resumed.messages, expected)

    def test_exit_at_compaction_commit_restores_checkpoint_before_next_request(self):
        script = '''
import json, os, sys
from pathlib import Path
from agent.agent import Agent
from configuration import Config
class Client:
    def complete(self, *args, **kwargs):
        return {"role": "assistant", "content": "<context_summary>durable checkpoint</context_summary>"}
agent = Agent(Config(base_url="http://example.test", api_key="", model="test",
    root_dir=Path(sys.argv[1]), state_dir=Path(sys.argv[2])), Client())
agent.run_request("retired raw input")
agent.run_request("kept raw input")
original_sync = os.fsync
def interrupt_after_commit(fd):
    original_sync(fd)
    events = [json.loads(line) for line in agent.store.path.read_text(encoding="utf-8").splitlines()]
    if events[-1]["type"] == "compact":
        os._exit(26)
os.fsync = interrupt_after_commit
agent.compact_now()
'''
        process = subprocess.run([sys.executable, "-c", script, str(self.root), str(self.state)],
                                 cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 26, process.stderr.decode(errors="replace"))
        info = SessionStore.list_sessions(self.config())[0]
        original = info.path.read_bytes()
        self.assertIn(b"retired raw input", original)
        client = FakeClient([{"role": "assistant", "content": "next answer"}])
        resumed = self.agent(client, resume=info.id)
        self.assertIn("[CONTEXT_COMPRESSED]", resumed.messages[1]["content"])
        with redirect_stdout(StringIO()):
            resumed.run_request("next input")
        self.assertNotIn("retired raw input", str(client.requests))
        self.assertIn("kept raw input", str(client.requests))
        self.assertTrue(info.path.read_bytes().startswith(original))

    def test_tool_started_failure_records_failed_status_but_keeps_context_after_resume(self):
        client = ScriptedClient([
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "write", "function": {"name": "edit", "arguments":
                    '{"path":"result.txt","content":"written once"}'}}]},
            ModelRequestError("answer failed"),
        ])
        agent = self.agent(client, tool_permission_mode="broad-access")
        with redirect_stdout(StringIO()):
            self.assertIsNone(agent.run_request("write result"))
        expected = deepcopy(agent.messages)
        records = self.records(agent.store.path)
        self.assertEqual(records[-1]["status"], "failed")
        self.assertTrue(records[-1]["context_valid"])
        session_id = agent.store.session_id
        agent.close()
        target = self.root / "result.txt"
        target.write_text("changed externally", encoding="utf-8")
        next_client = FakeClient([{"role": "assistant", "content": "acknowledged"}])
        resumed = self.agent(next_client, resume=session_id)
        self.assertEqual(resumed.messages, expected)
        with redirect_stdout(StringIO()):
            resumed.run_request("continue")
        self.assertIn("written once", str(next_client.requests))
        self.assertEqual(target.read_text(encoding="utf-8"), "changed externally")

    def test_exit_at_rollback_commit_excludes_input_without_losing_runtime_decisions(self):
        script = '''
import json, os, sys
from pathlib import Path
from agent.agent import Agent
from configuration import Config
from models.model_client import ModelRequestError
from tools.tool_runtime import ProviderLoadError
class Client:
    def complete(self, *args, **kwargs):
        agent.tool_runtime.policy.change_mode("approve-all")
        agent.tool_runtime.policy.revoke()
        raise ModelRequestError("model unavailable")
def native(*args):
    raise ProviderLoadError("unsupported")
agent = Agent(Config(base_url="http://example.test", api_key="", model="test",
    root_dir=Path(sys.argv[1]), state_dir=Path(sys.argv[2]),
    tool_permission_mode="broad-access", provider_tool_mode="native"), Client(), native_loader=native)
original_sync = os.fsync
def interrupt_after_commit(fd):
    original_sync(fd)
    events = [json.loads(line) for line in agent.store.path.read_text(encoding="utf-8").splitlines()]
    if events[-1]["type"] == "context_rollback":
        os._exit(25)
os.fsync = interrupt_after_commit
agent.run_request("failed raw input")
'''
        process = subprocess.run([sys.executable, "-c", script, str(self.root), str(self.state)],
                                 cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 25, process.stderr.decode(errors="replace"))
        info = SessionStore.list_sessions(self.config())[0]
        original = info.path.read_bytes()
        self.assertIn(b"failed raw input", original)
        client = FakeClient([{"role": "assistant", "content": "next answer"}])
        resumed = self.agent(client, resume=info.id, tool_permission_mode="broad-access")
        self.assertEqual(resumed.tool_runtime.policy.mode, "approve-all")
        self.assertTrue(resumed.tool_runtime.policy.revoked)
        self.assertEqual(resumed.provider_session.fallback_count, 1)
        self.assertEqual(resumed.provider_session.mode, "emulated")
        with redirect_stdout(StringIO()):
            resumed.run_request("next input")
        self.assertNotIn("failed raw input", str(client.requests))
        self.assertTrue(info.path.read_bytes().startswith(original))

    def test_exit_at_permission_commit_keeps_revocation_and_tightening(self):
        script = '''
import json, os, sys
from pathlib import Path
from agent.agent import Agent
from configuration import Config
agent = Agent(Config(base_url="http://example.test", api_key="", model="test",
    root_dir=Path(sys.argv[1]), state_dir=Path(sys.argv[2]),
    tool_permission_mode="broad-access"), object())
agent.tool_runtime.policy.change_mode("approve-all")
original_sync = os.fsync
def interrupt_after_commit(fd):
    original_sync(fd)
    events = [json.loads(line) for line in agent.store.path.read_text(encoding="utf-8").splitlines()]
    if any(e.get("event", e.get("audit", {})).get("action") == "revoked" for e in events):
        os._exit(24)
os.fsync = interrupt_after_commit
agent.tool_runtime.policy.revoke()
'''
        process = subprocess.run([sys.executable, "-c", script, str(self.root), str(self.state)],
                                 cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 24, process.stderr.decode(errors="replace"))
        info = SessionStore.list_sessions(self.config())[0]
        resumed = self.agent(FakeClient([]), resume=info.id, tool_permission_mode="broad-access")
        self.assertEqual(resumed.tool_runtime.policy.mode, "approve-all")
        self.assertTrue(resumed.tool_runtime.policy.revoked)
        self.assertEqual(sum(event.get("action") == "revoked" for event in resumed.store.load().audit), 1)

    def test_next_request_keeps_committed_checkpoint_live_and_after_restart(self):
        client = FakeClient([{"role": "assistant", "content": "old answer"},
                             {"role": "assistant", "content": "new answer"},
                             {"role": "assistant", "content": "<context_summary>checkpoint</context_summary>"},
                             {"role": "assistant", "content": "continued"}])
        agent = self.agent(client)
        with redirect_stdout(StringIO()):
            agent.run_request("old raw question")
            agent.run_request("new question")
            agent.compact_now()
        expected = deepcopy(agent.messages[1:])
        session_id = agent.store.session_id
        with redirect_stdout(StringIO()):
            agent.run_request("continue live")
        self.assertEqual(client.requests[-1][0][1:1 + len(expected)], expected)
        agent.close()
        resumed_client = FakeClient([{"role": "assistant", "content": "continued again"}])
        resumed = self.agent(resumed_client, resume=session_id)
        with redirect_stdout(StringIO()):
            resumed.run_request("continue resumed")
        self.assertEqual(resumed_client.requests[0][0][1:1 + len(expected)], expected)

    def test_compaction_write_failure_does_not_change_live_or_restarted_context(self):
        agent = self.agent(FakeClient([{"role": "assistant", "content": "old answer"},
                                       {"role": "assistant", "content": "new answer"}]))
        with redirect_stdout(StringIO()):
            agent.run_request("old question " * 100)
            agent.run_request("new question")
        before = deepcopy(agent.messages)
        session_id = agent.store.session_id
        with patch.object(agent.store, "record_compact", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                agent.compact_now()
        self.assertEqual(agent.messages, before)
        agent.close()
        resumed = self.agent(FakeClient([]), resume=session_id)
        self.assertEqual(resumed.messages, before)

    def test_context_rollback_preserves_security_and_audit_without_followup_writes(self):
        with SessionStore.create(self.config()) as store:
            store.record_runtime({"policy": {"mode": "broad-access"}})
            boundary = store.mark()
            store.record_task(1, "failed request")
            store.record_message({"role": "user", "content": "failed request"})
            store.record_runtime({"policy": {"mode": "approve-all", "revoked": True},
                                  "provider": {"mode": "emulated", "fallback_count": 1}})
            store.record_tool_audit({"action": "revoked"})
            original = store.path.read_bytes()
            store.truncate_to(boundary)
            contents = store.load()
            self.assertEqual(contents.messages, [])
            self.assertTrue(contents.runtime["policy"]["revoked"])
            self.assertEqual(contents.runtime["provider"]["fallback_count"], 1)
            self.assertEqual(contents.audit, [{"action": "revoked"}])
            self.assertTrue(store.path.read_bytes().startswith(original))

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
from agent.agent import Agent
from configuration import Config
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
        self.assertEqual(agent.store.history.tasks[-1]['event_id'], task['event_id'])
        self.assertFalse((self.state / 'memory').exists())
        agent.close()
        resumed = self.agent(FakeClient([]), resume=session_id)
        self.assertEqual(resumed.messages, expected)
        self.assertEqual(self.records(path), records)
