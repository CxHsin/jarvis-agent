import json
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path

from context_manager import (
    CONTEXT_COMPRESSED_MARKER,
    CONTEXT_RECOVERED_MARKER,
    ContextManager,
)
from jarvis_agent import Agent, Config, TOOL_DEFINITIONS, main
from session_store import SessionLockedError, SessionNotFoundError, SessionStore
from tests.test_jarvis_agent import FakeClient, FailingClient


class SessionTestBase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        base = Path(self.temporary.name)
        self.root = base / "workspace"
        self.root.mkdir()
        self.state = base / "state"
        self.agents: list[Agent] = []

    def tearDown(self):
        for agent in self.agents:
            agent.close()
        self.temporary.cleanup()

    def config(self, **kwargs):
        return Config(
            base_url="http://example.test/v1",
            api_key="",
            model="test",
            root_dir=self.root,
            state_dir=self.state,
            **kwargs,
        )

    def agent(self, client, resume=None, **kwargs):
        with redirect_stdout(StringIO()):
            agent = Agent(self.config(**kwargs), client, resume=resume)
        self.agents.append(agent)
        return agent

    def records(self, path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class SessionLifecycleTests(SessionTestBase):
    def test_default_start_creates_new_session_and_keeps_previous(self):
        first = self.agent(FakeClient([{"role": "assistant", "content": "第一次"}]))
        with redirect_stdout(StringIO()):
            self.assertEqual(first.run_request("第一段"), "第一次")
        first_id = first.store.session_id
        first.close()
        time.sleep(0.01)
        second = self.agent(FakeClient([{"role": "assistant", "content": "第二次"}]))
        with redirect_stdout(StringIO()):
            self.assertEqual(second.run_request("第二段"), "第二次")
        second_id = second.store.session_id
        second.close()
        self.assertNotEqual(first_id, second_id)

        sessions = SessionStore.list_sessions(self.config())
        self.assertEqual([session.id for session in sessions], [second_id, first_id])
        self.assertEqual(sessions[0].last_user_text, "第二段")
        self.assertEqual(sessions[0].message_count, 2)

        third = self.agent(FakeClient([]), resume="")
        self.assertEqual(third.store.session_id, second_id)

    def test_resume_without_history_is_reported(self):
        with self.assertRaises(SessionNotFoundError):
            SessionStore.resume(self.config())
        with self.assertRaises(SessionNotFoundError):
            SessionStore.resume(self.config(), "20260911-000000-ffff")

    def test_concurrent_resume_is_rejected(self):
        store = SessionStore.create(self.config())
        session_id = store.session_id
        with self.assertRaises(SessionLockedError):
            SessionStore.resume(self.config(), session_id)
        store.close()
        reused = SessionStore.resume(self.config(), session_id)
        reused.close()

    def test_cli_lists_sessions_and_reports_missing_resume(self):
        env_file = Path(self.temporary.name) / "cli.env"
        env_file.write_text(
            "BASE_URL=http://example.test/v1\n"
            "MODEL=test\n"
            "API_KEY=\n"
            f"ROOT_DIR={self.root}\n"
            f"STATE_DIR={self.state}\n",
            encoding="utf-8",
        )
        store = SessionStore.create(self.config())
        session_id = store.session_id
        store.record_message({"role": "user", "content": "命令行列举"})
        store.close()

        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--env-file", str(env_file), "--list"]), 0)
        self.assertIn(session_id, output.getvalue())
        self.assertIn("命令行列举", output.getvalue())

        empty_env = Path(self.temporary.name) / "empty.env"
        empty_root = Path(self.temporary.name) / "empty-workspace"
        empty_root.mkdir()
        empty_env.write_text(
            "BASE_URL=http://example.test/v1\n"
            "MODEL=test\n"
            f"ROOT_DIR={empty_root}\n"
            f"STATE_DIR={Path(self.temporary.name) / 'empty-state'}\n",
            encoding="utf-8",
        )
        errors = StringIO()
        with redirect_stderr(errors):
            self.assertEqual(main(["--env-file", str(empty_env), "--resume"]), 2)
        self.assertIn("会话错误", errors.getvalue())


class SessionRecoveryTests(SessionTestBase):
    def test_resume_restores_history_and_evidence_index(self):
        (self.root / "note.md").write_text("agent loop\n第二行\n", encoding="utf-8")
        client = FakeClient(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "function": {"name": "read_file", "arguments": json.dumps({"path": "note.md"})},
                        }
                    ],
                },
                {"role": "assistant", "content": "读完了"},
            ]
        )
        agent = self.agent(client)
        with redirect_stdout(StringIO()):
            self.assertEqual(agent.run_request("读笔记"), "读完了")
        expected_messages = deepcopy(agent.messages)
        expected_evidence = deepcopy(agent.context.session_evidence)
        session_id = agent.store.session_id
        agent.close()

        resumed = self.agent(FakeClient([]), resume=session_id)
        self.assertEqual(resumed.messages, expected_messages)
        self.assertEqual(resumed.context.session_evidence, expected_evidence)
        self.assertEqual(resumed.context.task_number, 1)
        self.assertEqual(resumed.context.session_archive[0]["result"]["path"], "note.md")

        with redirect_stdout(StringIO()):
            prepared = resumed.context.prepare_messages(
                resumed.messages, TOOL_DEFINITIONS, resumed.compression_client
            )
        self.assertEqual(
            [message["role"] for message in prepared],
            [message["role"] for message in resumed.messages],
        )
        self.assertNotIn("<agent_status>", "\n".join(str(message) for message in prepared))

    def test_interrupted_tool_call_is_repaired_without_replay(self):
        (self.root / "note.md").write_text("第一份\n", encoding="utf-8")
        big = "事实 " * 160
        store = SessionStore.create(self.config())
        session_id = store.session_id
        store.record_task(1, "中断测试")
        store.record_message({"role": "user", "content": "同时读两份笔记"})
        store.record_message(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call-a", "function": {"name": "read_file", "arguments": '{"path":"note.md"}'}},
                    {"id": "call-b", "function": {"name": "read_file", "arguments": '{"path":"other.md"}'}},
                ],
            }
        )
        first_result = {"ok": True, "path": "note.md", "start_line": 1, "end_line": 1, "content": big}
        store.record_archive(
            {
                "task": 1,
                "call_id": "call-a",
                "tool": "read_file",
                "arguments": {"path": "note.md"},
                "result": first_result,
                "evidence_id": "E1",
                "compressed": False,
            }
        )
        store.record_message(
            {"role": "tool", "tool_call_id": "call-a", "name": "read_file", "content": json.dumps(first_result, ensure_ascii=False)}
        )
        path = store.path
        store.close()

        output = StringIO()
        with redirect_stdout(output):
            agent = Agent(self.config(), FakeClient([]), resume=session_id)
        self.agents.append(agent)

        self.assertEqual([message["role"] for message in agent.messages], ["system", "user", "assistant", "tool", "tool"])
        repaired = agent.messages[-1]
        self.assertEqual(repaired["tool_call_id"], "call-b")
        self.assertTrue(repaired["content"].startswith(CONTEXT_RECOVERED_MARKER))
        self.assertIn("call-b", output.getvalue())
        self.assertEqual(len(agent.context.session_evidence), 1)

        records = self.records(path)
        self.assertEqual(records[-1]["type"], "message")
        self.assertEqual(records[-1]["message"]["tool_call_id"], "call-b")
        self.assertTrue(records[-1]["message"]["content"].startswith(CONTEXT_RECOVERED_MARKER))

    def test_truncated_tail_is_dropped_and_bad_lines_skipped(self):
        store = SessionStore.create(self.config())
        session_id = store.session_id
        path = store.path
        store.record_message({"role": "user", "content": "完整"})
        store.close()

        text = path.read_text(encoding="utf-8")
        text += "not-json\n"
        text += json.dumps({"type": "message", "message": {"role": "user", "content": "之后"}}, ensure_ascii=False)
        text += "\n"
        text += '{"type":"message","message":{"role":"user","content":"写了一半'
        path.write_text(text, encoding="utf-8")

        reopened = SessionStore.resume(self.config(), session_id)
        try:
            contents = reopened.load()
        finally:
            reopened.close()
        self.assertEqual([message["content"] for message in contents.messages], ["完整", "之后"])
        self.assertEqual(len(contents.warnings), 2)
        self.assertIn("写了一半", " ".join(contents.warnings))


class SessionWriteThroughTests(SessionTestBase):
    def test_model_failure_rolls_back_memory_and_file(self):
        agent = self.agent(FakeClient([]))
        path = agent.store.path
        with redirect_stdout(StringIO()):
            agent.client.client = FailingClient()
            self.assertIsNone(agent.run_request("失败请求"))

        self.assertEqual([message["role"] for message in agent.messages], ["system"])
        self.assertEqual(agent.context.task_number, 0)
        self.assertEqual([record["type"] for record in self.records(path)], ["session"])

        # 回滚后的会话必须还能继续写入，且重载内容与内存一致。
        with redirect_stdout(StringIO()):
            agent.client.client = FakeClient([{"role": "assistant", "content": "重试成功"}])
            self.assertEqual(agent.run_request("重试"), "重试成功")
        session_id = agent.store.session_id
        expected = deepcopy(agent.messages)
        agent.close()

        resumed = self.agent(FakeClient([]), resume=session_id)
        self.assertEqual(resumed.messages, expected)

    def test_compression_replacements_persist_and_rebuild_original_archive(self):
        config = self.config(
            context_window_tokens=160,
            context_compression_threshold=0.86,
            context_compression_target=0.65,
        )
        store = SessionStore.create(config)
        session_id = store.session_id
        manager = ContextManager(config, recorder=store)
        manager.begin_task("压缩测试")
        original = "事实 " * 160
        result = {"ok": True, "path": "a.md", "start_line": 1, "end_line": 20, "content": original}
        manager.record_tool_result("read_file", {"path": "a.md"}, result, "call-1")
        messages = [
            {"role": "user", "content": "找结论"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call-1", "function": {"name": "read_file", "arguments": '{"path":"a.md"}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "name": "read_file", "content": json.dumps(result, ensure_ascii=False)},
        ]
        for message in messages:
            store.record_message(message)
        compression_client = FakeClient(
            [{"role": "assistant", "content": "<context_summary>来自 a.md 第 1-20 行的事实。</context_summary>"}]
        )
        with redirect_stdout(StringIO()):
            manager.prepare_messages(messages, TOOL_DEFINITIONS, compression_client)
        self.assertIn(CONTEXT_COMPRESSED_MARKER, messages[2]["content"])
        store.close()

        reopened = SessionStore.resume(config, session_id)
        try:
            contents = reopened.load()
        finally:
            reopened.close()
        self.assertIn(CONTEXT_COMPRESSED_MARKER, contents.messages[2]["content"])

        restored = ContextManager(config)
        restored.task_number = contents.task_number
        restored.restore_session(contents.archive, list(contents.replacements))
        self.assertEqual(restored.session_archive[0]["result"]["content"], original)
        self.assertTrue(restored.session_archive[0]["compressed"])
        self.assertTrue(restored.session_evidence[0]["compressed"])
        self.assertEqual(restored.session_evidence[0]["id"], "E1")

    def test_snapshot_restore_undoes_compression_state(self):
        config = self.config(
            context_window_tokens=160,
            context_compression_threshold=0.86,
            context_compression_target=0.65,
        )
        manager = ContextManager(config)
        manager.begin_task("回滚测试")
        result = {"ok": True, "path": "a.md", "start_line": 1, "end_line": 20, "content": "事实 " * 160}
        manager.record_tool_result("read_file", {"path": "a.md"}, result, "call-1")
        snapshot = manager.snapshot()
        messages = [
            {"role": "user", "content": "找结论"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call-1", "function": {"name": "read_file", "arguments": '{"path":"a.md"}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "name": "read_file", "content": json.dumps(result, ensure_ascii=False)},
        ]
        compression_client = FakeClient(
            [{"role": "assistant", "content": "<context_summary>摘要</context_summary>"}]
        )
        with redirect_stdout(StringIO()):
            manager.prepare_messages(messages, TOOL_DEFINITIONS, compression_client)
        self.assertTrue(manager.session_archive[0]["compressed"])
        self.assertTrue(manager.session_evidence[0]["compressed"])

        manager.restore(snapshot)
        self.assertFalse(manager.session_archive[0]["compressed"])
        self.assertFalse(manager.session_evidence[0]["compressed"])
        self.assertEqual(manager.current_task["goal"], "回滚测试")


if __name__ == "__main__":
    unittest.main()
