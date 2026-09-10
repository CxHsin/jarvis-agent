import json
import tempfile
import unittest
from pathlib import Path

from jarvis_agent import Config, Workspace, Agent, ModelRequestError


class FakeClient:
    def __init__(self, messages):
        self.responses = iter(messages)
        self.calls = []

    def complete(self, messages, tools, tool_choice):
        self.calls.append((len(messages), len(tools), tool_choice))
        return next(self.responses)


class FailingClient:
    def complete(self, messages, tools, tool_choice):
        raise ModelRequestError("test failure")


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
    def test_model_failure_discards_incomplete_request(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(base_url="http://example.test/v1", api_key="", model="test", root_dir=Path(directory))
            agent = Agent(config, FailingClient())
            self.assertIsNone(agent.run_request("接口失败测试"))
            self.assertEqual([message["role"] for message in agent.messages], ["system"])

    def test_tool_calls_are_executed_in_order_and_final_round_has_no_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note.md").write_text("agent loop", encoding="utf-8")
            config = Config(base_url="http://example.test/v1", api_key="", model="test", root_dir=root, max_rounds=2)
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
            agent = Agent(config, client)
            answer = agent.run_request("找找 agent")
            self.assertEqual(answer, "找到了 note.md。")
            self.assertEqual(client.calls, [(2, 3, "auto"), (5, 0, "none")])
            self.assertEqual([message["role"] for message in agent.messages], ["system", "user", "assistant", "tool", "tool", "assistant"])

    def test_cancelled_tool_calls_are_marked(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(base_url="http://example.test/v1", api_key="", model="test", root_dir=Path(directory))
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
            agent = Agent(config, client)
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
