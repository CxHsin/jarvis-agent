import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from context_manager import CONTEXT_COMPRESSED_MARKER, ContextManager
from jarvis_agent import ChatCompletionsClient, Config, Workspace, Agent, ModelRequestError, TOOL_DEFINITIONS


_STATE_ROOT = Path(tempfile.mkdtemp(prefix="jarvis-test-state-"))


def make_agent(case, config, client=None, **kwargs):
    """Build an Agent whose session file is closed when the test ends."""

    agent = Agent(config, client, **kwargs)
    case.addCleanup(agent.close)
    return agent


class FakeClient:
    def __init__(self, messages):
        self.responses = iter(messages)
        self.calls = []
        self.requests = []

    def complete(self, messages, tools, tool_choice):
        self.calls.append((len(messages), len(tools), tool_choice))
        self.requests.append(([dict(message) for message in messages], list(tools), tool_choice))
        return next(self.responses)


class FailingClient:
    def complete(self, messages, tools, tool_choice):
        raise ModelRequestError("test failure")


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class CancellingWorkspace:
    def list_directory(self, path="."):
        raise KeyboardInterrupt

    def search_file_content(self, query, path="."):
        return {"ok": True, "matches": []}

    def read_file(self, path, start_line=1, end_line=None):
        return {"ok": True, "content": ""}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        (root / "notes").mkdir()
        (root / "notes" / "one.md").write_text("先找到问题\n然后自己试验\n", encoding="utf-8")
        (root / "video.mp4").write_bytes(b"not text")
        self.config = Config(base_url="http://example.test/v1", api_key="", model="test", root_dir=root)
        self.workspace = Workspace(self.config)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_search_and_read_return_evidence(self):
        result = self.workspace.search_file_content("自己试验")
        self.assertEqual(result["matches"][0]["line"], 2)
        content = self.workspace.read_file("notes/one.md", 2, 2)
        self.assertEqual(content["content"], "2: 然后自己试验")

    def test_path_cannot_escape_root(self):
        with self.assertRaises(ValueError):
            self.workspace.read_file("../outside.md")

    def test_binary_extension_is_rejected(self):
        with self.assertRaises(ValueError):
            self.workspace.read_file("video.mp4")


class AgentTests(unittest.TestCase):
    def test_context_window_can_be_discovered_from_upstream_model_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                base_url="http://example.test/v1",
                api_key="secret",
                model="test-model",
                root_dir=Path(directory),
            )
            client = ChatCompletionsClient(config)
            with patch("jarvis_agent.urllib.request.urlopen", return_value=FakeHTTPResponse({"context_length": 32768})) as urlopen:
                self.assertEqual(client.discover_context_window(), 32768)
            request = urlopen.call_args.args[0]
            self.assertEqual(request.full_url, "http://example.test/v1/models/test-model")
            self.assertEqual(request.get_header("Authorization"), "Bearer secret")

    def test_agent_uses_upstream_context_window_when_not_explicitly_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                base_url="http://example.test/v1",
                api_key="",
                model="test-model",
                max_output_tokens=1024,
                root_dir=Path(directory),
                state_dir=_STATE_ROOT,
            )
            client = ChatCompletionsClient(config)
            with patch("jarvis_agent.urllib.request.urlopen", return_value=FakeHTTPResponse({"data": [{"id": "test-model", "max_model_len": 65536}]})):
                agent = make_agent(self, config, client)
            self.assertEqual(agent.config.context_window_tokens, 65536)
            self.assertEqual(agent.config.context_window_source, "upstream")

    def test_explicit_context_window_skips_upstream_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                base_url="http://example.test/v1",
                api_key="",
                model="test-model",
                max_output_tokens=1024,
                root_dir=Path(directory),
                context_window_tokens=16384,
                context_window_source="configured",
                state_dir=_STATE_ROOT,
            )
            client = ChatCompletionsClient(config)
            with patch("jarvis_agent.urllib.request.urlopen") as urlopen:
                agent = make_agent(self, config, client)
            self.assertEqual(agent.config.context_window_tokens, 16384)
            self.assertEqual(agent.config.context_window_source, "configured")
            urlopen.assert_not_called()

    def test_model_failure_discards_incomplete_request(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(base_url="http://example.test/v1", api_key="", model="test", root_dir=Path(directory),
                            state_dir=_STATE_ROOT)
            agent = make_agent(self, config, FailingClient())
            self.assertIsNone(agent.run_request("接口失败测试"))
            self.assertEqual([message["role"] for message in agent.messages], ["system"])

    def test_tool_calls_are_executed_in_order_and_final_round_has_no_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note.md").write_text("agent loop", encoding="utf-8")
            config = Config(base_url="http://example.test/v1", api_key="", model="test", root_dir=root, max_rounds=2,
                            state_dir=_STATE_ROOT)
            client = FakeClient(
                [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "1", "function": {"name": "list_directory", "arguments": "{}"}},
                            {"id": "2", "function": {"name": "search_file_content", "arguments": json.dumps({"query": "agent"})}},
                        ],
                    },
                    {"role": "assistant", "content": "找到了 note.md。"},
                ]
            )
            agent = make_agent(self, config, client)
            answer = agent.run_request("找找 agent")
            self.assertEqual(answer, "找到了 note.md。")
            self.assertEqual(client.calls, [(2, 3, "auto"), (5, 0, "none")])
            self.assertEqual([message["role"] for message in agent.messages], ["system", "user", "assistant", "tool", "tool", "assistant"])

    def test_no_status_bar_is_appended_to_model_context(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(base_url="http://example.test/v1", api_key="", model="test", root_dir=Path(directory),
                            state_dir=_STATE_ROOT)
            client = FakeClient([{"role": "assistant", "content": "完成"}])
            agent = make_agent(self, config, client)

            self.assertEqual(agent.run_request("检查上下文"), "完成")
            request_messages = client.requests[0][0]
            self.assertEqual([message["role"] for message in request_messages], ["system", "user"])
            self.assertEqual(request_messages[-1]["content"], "检查上下文")
            self.assertNotIn("<agent_status>", "\n".join(str(message) for message in request_messages))

    def test_tool_result_terminal_output_is_compact_but_model_result_stays_full(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "large.md").write_text("重要内容\n" * 200, encoding="utf-8")
            config = Config(
                base_url="http://example.test/v1",
                api_key="",
                model="test",
                root_dir=root,
                max_rounds=2,
                tool_output_preview_chars=120,
                state_dir=_STATE_ROOT,
            )
            client = FakeClient(
                [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "1", "function": {"name": "read_file", "arguments": '{"path":"large.md"}'}},
                        ],
                    },
                    {"role": "assistant", "content": "已读取。"},
                ]
            )
            agent = make_agent(self, config, client)
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(agent.run_request("读取大文件"), "已读取。")

            terminal = output.getvalue()
            self.assertIn("preview=", terminal)
            self.assertIn("...", terminal)
            self.assertLess(len(terminal), 2_000)
            tool_message = next(message for message in agent.messages if message["role"] == "tool")
            self.assertGreater(len(tool_message["content"]), 1_000)

    def test_context_manager_records_repeated_evidence_without_creating_new_entry(self):
        config = Config(
            base_url="http://example.test/v1",
            api_key="",
            model="test",
            root_dir=Path.cwd(),
            context_window_tokens=10_000,
        )
        manager = ContextManager(config)
        manager.begin_task("查找证据")
        result = {"ok": True, "path": "note.md", "start_line": 1, "end_line": 2, "content": "1: a\n2: b"}
        manager.record_tool_result("read_file", {"path": "note.md", "start_line": 1, "end_line": 2}, result, "1")
        manager.record_tool_result("read_file", {"path": "note.md", "start_line": 1, "end_line": 2}, result, "2")

        self.assertEqual(len(manager.session_evidence), 1)
        self.assertEqual(manager.current_task["repeated_calls"], 1)
        self.assertTrue(manager.session_evidence[0]["repeated"])

    def test_context_compression_replaces_old_tool_results_and_keeps_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                base_url="http://example.test/v1",
                api_key="",
                model="test",
                root_dir=Path(directory),
                context_window_tokens=160,
                context_compression_threshold=0.86,
                context_compression_target=0.65,
            )
            manager = ContextManager(config)
            manager.begin_task("从文件中找出结论")
            manager.record_tool_result(
                "read_file",
                {"path": "a.md"},
                {"ok": True, "path": "a.md", "start_line": 1, "end_line": 20, "content": "事实 " * 160},
                "call-1",
            )
            messages = [
                {"role": "system", "content": "stable"},
                {"role": "user", "content": "找结论"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "call-1", "function": {"name": "read_file", "arguments": '{"path":"a.md"}'}}],
                },
                {"role": "tool", "tool_call_id": "call-1", "name": "read_file", "content": "事实 " * 160},
            ]
            compression_client = FakeClient(
                [{"role": "assistant", "content": "<context_summary>来自 a.md 第 1-20 行的事实。</context_summary>"}]
            )

            prepared = manager.prepare_messages(messages, TOOL_DEFINITIONS, compression_client)

            self.assertIsNotNone(manager.last_compression_event)
            self.assertIn(CONTEXT_COMPRESSED_MARKER, messages[3]["content"])
            self.assertEqual(len(manager.session_archive), 1)
            self.assertTrue(manager.session_archive[0]["compressed"])
            self.assertEqual([message["role"] for message in prepared], ["system", "user", "assistant", "tool"])

    def test_cancelled_tool_calls_are_marked(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(base_url="http://example.test/v1", api_key="", model="test", root_dir=Path(directory),
                            state_dir=_STATE_ROOT)
            client = FakeClient(
                [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "1", "function": {"name": "list_directory", "arguments": "{}"}},
                            {"id": "2", "function": {"name": "search_file_content", "arguments": '{"query":"x"}'}},
                        ],
                    }
                ]
            )
            agent = make_agent(self, config, client)
            agent.workspace = CancellingWorkspace()
            agent.tool_functions = {
                "list_directory": agent.workspace.list_directory,
                "search_file_content": agent.workspace.search_file_content,
                "read_file": agent.workspace.read_file,
            }
            self.assertIsNone(agent.run_request("取消测试"))
            tool_messages = [message for message in agent.messages if message["role"] == "tool"]
            self.assertEqual(len(tool_messages), 2)
            self.assertTrue(all('"cancelled": true' in message["content"] for message in tool_messages))


if __name__ == "__main__":
    unittest.main()
