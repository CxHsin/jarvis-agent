import unittest
from pathlib import Path

from context.compaction import is_overflow_error
from context.context_manager import CONTEXT_COMPRESSED_MARKER, ContextManager
from configuration import Config
from tests.test_jarvis_agent import FakeClient


def make_manager(**kwargs):
    kwargs.setdefault("context_keep_recent_tokens", 500)
    kwargs.setdefault("context_compaction_failure_limit", 3)
    kwargs.setdefault("context_reserve_tokens", 1000)
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
    def test_overflow_error_is_recognised_by_message_text(self):
        # Both bodies were recorded in ADR 0005 against the real endpoint: the
        # provider uses the same type, code and null param for both, so only the
        # message text distinguishes an overflow from an invalid max_tokens.
        overflow = (
            '模型接口返回 HTTP 400: {"error":{"message":"This model\'s maximum context length is 1048576 tokens. '
            "However, you requested 1166688 tokens (1166687 in the messages, 1 in the completion). "
            'Please reduce the length of the messages or completion.","type":"invalid_request_error",'
            '"param":null,"code":"invalid_request_error"}}'
        )
        invalid_output = (
            '模型接口返回 HTTP 400: {"error":{"message":"Invalid max_tokens value, the valid range of max_tokens '
            'is [1, 393216]","type":"invalid_request_error","param":null,"code":"invalid_request_error"}}'
        )
        self.assertTrue(is_overflow_error(overflow))
        self.assertFalse(is_overflow_error(invalid_output))
        self.assertFalse(is_overflow_error("无法连接模型接口: timed out"))

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
                context_reserve_tokens=1000,
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

    def test_manual_compaction_retires_older_tasks_below_the_keep_window(self):
        # 用户报告：聊了几轮、总量远小于保留窗口时 /compact 什么也不做。
        manager = make_manager(context_keep_recent_tokens=100_000)
        manager.begin_task("任务一")
        manager.begin_task("任务二")
        messages = [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "任务一"},
            {"role": "assistant", "content": "第一次完成"},
            {"role": "user", "content": "任务二"},
            {"role": "assistant", "content": "第二次完成"},
        ]
        compressor = FakeClient([{"role": "assistant", "content": "<context_summary>任务一完成</context_summary>"}])

        result = manager.compact(messages, [], compressor, "manual")

        self.assertTrue(result.compacted)
        self.assertEqual(result.cut_index, 3)
        self.assertEqual(result.retired_messages, 2)
        self.assertEqual(result.keep_tokens, 100_000)
        self.assertEqual([message["role"] for message in messages], ["system", "user", "user", "assistant"])
        self.assertEqual(messages[2]["content"], "任务二")
        self.assertIn("任务一完成", messages[1]["content"])

    def test_manual_compaction_honours_an_explicit_keep_target(self):
        manager = make_manager(context_keep_recent_tokens=100_000)
        manager.begin_task("任务一")
        messages = [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "任务一"},
            {"role": "assistant", "content": "第一轮" + "x" * 400},
            {"role": "user", "content": "任务二"},
            {"role": "assistant", "content": "第二轮" + "y" * 400},
        ]
        compressor = FakeClient([{"role": "assistant", "content": "<context_summary>摘要</context_summary>"}])

        result = manager.compact(messages, [], compressor, "manual", keep_tokens=50)

        self.assertTrue(result.compacted)
        self.assertEqual(result.keep_tokens, 50)
        self.assertEqual(result.cut_index, 4)
        self.assertEqual([message["role"] for message in messages], ["system", "user", "assistant"])

    def test_manual_compaction_without_older_tasks_reports_the_numbers(self):
        manager = make_manager(context_keep_recent_tokens=100_000)
        manager.begin_task("唯一任务")
        messages = [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "唯一任务"},
            {"role": "assistant", "content": "完成"},
        ]
        compressor = FakeClient([{"role": "assistant", "content": "<context_summary>摘要</context_summary>"}])

        result = manager.compact(messages, [], compressor, "manual")

        self.assertFalse(result.compacted)
        self.assertIn("没有超出保留窗口", result.warning)
        self.assertIn("100000", result.warning)
        self.assertEqual(len(compressor.requests), 0)


if __name__ == "__main__":
    unittest.main()
