import unittest
from pathlib import Path

from context_manager import CONTEXT_COMPRESSED_MARKER, ContextManager
from jarvis_agent import Config
from tests.test_jarvis_agent import FakeClient


def make_manager(**kwargs):
    kwargs.setdefault("context_keep_recent_tokens", 500)
    kwargs.setdefault("context_compaction_failure_limit", 3)
    config = Config(
        base_url="http://example.test/v1",
        api_key="",
        model="test",
        root_dir=Path.cwd(),
        context_window_tokens=10000,
        **kwargs,
    )
    return ContextManager(config)


class CompactionCutPointTests(unittest.TestCase):
    def test_split_turn_keeps_recent_round_and_retires_earlier_round(self):
        manager = make_manager()
        manager.begin_task("长任务")
        result = {
            "ok": True,
            "path": "a.md",
            "start_line": 1,
            "end_line": 20,
            "content": "x" * 2000,
        }
        manager.record_tool_result("read_file", {"path": "a.md"}, result, "c1")
        manager.record_tool_result("read_file", {"path": "a.md"}, result, "c2")
        messages = [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "长任务"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"a.md"}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "x" * 2000},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c2", "function": {"name": "read_file", "arguments": '{"path":"a.md"}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "c2", "name": "read_file", "content": "x" * 2000},
        ]
        compressor = FakeClient(
            [{"role": "assistant", "content": "<context_summary>前半段事实</context_summary>"}]
        )

        manager.compact(messages, [], compressor)

        self.assertEqual(manager.last_compression_event.cut_index, 4)
        self.assertEqual(manager.last_compression_event.compressed_call_ids, ("c1",))
        self.assertEqual(
            [message["role"] for message in messages],
            ["system", "user", "assistant", "tool"],
        )
        self.assertIn(CONTEXT_COMPRESSED_MARKER, messages[1]["content"])
        self.assertFalse(manager.session_archive[1]["compressed"])

    def test_compression_serializes_assistant_prose(self):
        manager = make_manager(context_keep_recent_tokens=1)
        manager.begin_task("任务")
        messages = [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "任务一"},
            {"role": "assistant", "content": "我先读一下文件"},
            {"role": "assistant", "content": "任务一完成"},
            {"role": "user", "content": "任务二"},
        ]

        class CapturingCompressor:
            def __init__(self):
                self.prompts = []

            def complete(self, messages, tools, tool_choice):
                self.prompts.append(messages)
                return {"content": "<context_summary>完成</context_summary>"}

        compressor = CapturingCompressor()
        manager.compact(messages, [], compressor)
        prompt = compressor.prompts[0][-1]["content"]
        self.assertIn("[Assistant]: 我先读一下文件", prompt)
        self.assertIn("[Assistant]: 任务一完成", prompt)

    def test_second_compaction_passes_previous_summary_as_context(self):
        manager = make_manager(context_keep_recent_tokens=1)
        manager.begin_task("任务")
        compressor = FakeClient(
            [
                {"role": "assistant", "content": "<context_summary>第一批摘要</context_summary>"},
                {"role": "assistant", "content": "<context_summary>第二批摘要</context_summary>"},
            ]
        )
        messages = [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "任务一"},
            {"role": "assistant", "content": "第一批完成"},
            {"role": "user", "content": "任务二"},
        ]

        manager.compact(messages, [], compressor)
        self.assertIn(CONTEXT_COMPRESSED_MARKER, messages[1]["content"])
        messages.extend(
            [
                {"role": "assistant", "content": "第二批完成"},
                {"role": "user", "content": "任务三"},
            ]
        )
        manager.compact(messages, [], compressor)

        second_prompt = compressor.requests[1][0][-1]["content"]
        self.assertIn("第一批摘要", second_prompt)

    def test_circuit_breaker_stops_after_consecutive_failures(self):
        manager = make_manager(context_keep_recent_tokens=1)
        manager.begin_task("任务")

        class InvalidCompressor:
            def __init__(self):
                self.calls = 0

            def complete(self, messages, tools, tool_choice):
                self.calls += 1
                return {"content": "没有 summary"}

        def fresh_messages():
            return [
                {"role": "system", "content": "stable"},
                {"role": "user", "content": "任务一"},
                {"role": "assistant", "content": "第一批完成"},
                {"role": "user", "content": "任务二"},
            ]

        compressor = InvalidCompressor()
        for _ in range(3):
            manager.compact(fresh_messages(), [], compressor)
        self.assertEqual(compressor.calls, 3)
        self.assertEqual(manager.compaction_failures, 3)

        manager.compact(fresh_messages(), [], compressor)
        self.assertEqual(compressor.calls, 3)
        self.assertIn("熔断", manager.last_compression_event.warning)

    def test_auto_and_manual_compaction_produce_the_same_effect(self):
        class Recorder:
            def __init__(self):
                self.compacts = []

            def record_task(self, number, goal):
                pass

            def record_compact(self, kept_from, content, method, call_ids=(), reason="auto"):
                self.compacts.append({"kept_from": kept_from, "content": content, "method": method,
                                      "call_ids": tuple(call_ids), "reason": reason})

        def run(reason):
            config = Config(
                base_url="http://example.test/v1",
                api_key="",
                model="test",
                root_dir=Path.cwd(),
                context_window_tokens=10000,
                context_keep_recent_tokens=1,
            )
            recorder = Recorder()
            manager = ContextManager(config, recorder)
            manager.begin_task("任务")
            messages = [
                {"role": "system", "content": "stable"},
                {"role": "user", "content": "任务一"},
                {"role": "assistant", "content": "第一批完成"},
                {"role": "user", "content": "任务二"},
            ]
            compressor = FakeClient(
                [{"role": "assistant", "content": "<context_summary>摘要</context_summary>"}]
            )
            result = manager.compact(messages, [], compressor, reason)
            return messages, result, recorder.compacts

        auto_messages, auto_result, auto_records = run("auto")
        manual_messages, manual_result, manual_records = run("manual")

        self.assertEqual(auto_messages, manual_messages)
        self.assertEqual(auto_result.method, manual_result.method)
        self.assertEqual(auto_result.cut_index, manual_result.cut_index)
        self.assertEqual(auto_result.after_tokens, manual_result.after_tokens)
        self.assertEqual(len(auto_records), len(manual_records))
        self.assertEqual(
            [{key: value for key, value in record.items() if key != "reason"} for record in auto_records],
            [{key: value for key, value in record.items() if key != "reason"} for record in manual_records],
        )
        self.assertEqual(auto_records[0]["reason"], "auto")
        self.assertEqual(manual_records[0]["reason"], "manual")

    def test_manual_compaction_without_old_content_changes_nothing(self):
        manager = make_manager(context_keep_recent_tokens=1000)
        manager.begin_task("任务")
        messages = [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "任务一"},
        ]
        compressor = FakeClient([{"role": "assistant", "content": "<context_summary>摘要</context_summary>"}])

        result = manager.compact(messages, [], compressor, "manual")

        self.assertFalse(result.compacted)
        self.assertEqual(result.method, "none")
        self.assertEqual(len(compressor.requests), 0)
        self.assertEqual([message["content"] for message in messages], ["stable", "任务一"])


if __name__ == "__main__":
    unittest.main()
