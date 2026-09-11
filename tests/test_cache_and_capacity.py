import json
import tempfile
import unittest
from dataclasses import replace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from cache_metrics import cache_usage, MeasuredClient, UsageLedger
from context_manager import ContextManager, estimate_tokens
from jarvis_agent import Config, ConfigurationError, ChatCompletionsClient, Agent, ModelRequestError
from model_capabilities import load_capability
from tests.test_jarvis_agent import FakeHTTPResponse, FakeClient, make_agent


_STATE_ROOT = Path(tempfile.mkdtemp(prefix="jarvis-test-state-"))


class CacheTests(unittest.TestCase):
    def test_deepseek_alias_fields_are_not_double_counted(self):
        metrics = cache_usage({"prompt_tokens": 1000, "prompt_cache_hit_tokens": 800,
                               "prompt_cache_miss_tokens": 200,
                               "prompt_tokens_details": {"cached_tokens": 800}})
        self.assertEqual((metrics["input"], metrics["hit"], metrics["ratio"]), (1000, 800, .8))

    def test_unknown_zero_and_invalid_counts(self):
        self.assertIsNone(cache_usage({"prompt_tokens": 10})["hit"])
        self.assertEqual(cache_usage({"prompt_tokens": 10, "prompt_cache_hit_tokens": 0})["ratio"], 0)
        for bad in (True, -1, "5", 1.5, 11):
            self.assertIsNone(cache_usage({"prompt_tokens": 10, "prompt_cache_hit_tokens": bad})["hit"])
        self.assertIsNone(cache_usage({"prompt_tokens": 10, "prompt_cache_hit_tokens": 5,
                                       "prompt_cache_miss_tokens": 8})["ratio"])
        self.assertEqual(cache_usage({"prompt_cache_hit_tokens": 8, "prompt_cache_miss_tokens": 2})["input"], 10)

    def test_weighted_summary_and_incomplete_coverage(self):
        ledger = UsageLedger()
        with redirect_stdout(StringIO()):
            ledger.record("主模型", "m", {"prompt_tokens": 100, "prompt_cache_hit_tokens": 100})
            ledger.record("主模型", "m", {"prompt_tokens": 900, "prompt_cache_hit_tokens": 0})
        output = StringIO()
        with redirect_stdout(output):
            ledger.summary()
        self.assertIn("占比=10.0%", output.getvalue())
        with redirect_stdout(StringIO()):
            ledger.record("主模型", "m", None, failed=True)
        output = StringIO()
        with redirect_stdout(output):
            ledger.summary()
        self.assertIn("指标完整=2/3", output.getvalue())
        self.assertIn("占比=未知", output.getvalue())

    def test_shared_client_roles_and_missing_usage_do_not_leak(self):
        class Client:
            last_usage = None
            count = 0
            def complete(self, *args):
                self.count += 1
                if self.count < 3:
                    self.last_usage = {"prompt_tokens": 100, "prompt_cache_hit_tokens": self.count}
                return {"content": "ok"}
        raw, ledger = Client(), UsageLedger()
        main = MeasuredClient(raw, ledger, "主模型", "m")
        compression = MeasuredClient(raw, ledger, "压缩模型", "m")
        with redirect_stdout(StringIO()):
            main.complete([], [])
            compression.complete([], [])
            self.assertEqual(main.last_usage["prompt_cache_hit_tokens"], 1)
            main.complete([], [])
        self.assertEqual([r["role"] for r in ledger.records], ["主模型", "压缩模型", "主模型"])
        self.assertIsNone(ledger.records[-1]["hit"])


class CapacityTests(unittest.TestCase):
    def config(self, **kwargs):
        return Config(base_url="https://example.test/v1", api_key="", model="test",
                      max_output_tokens=1000, state_dir=_STATE_ROOT, **kwargs)

    def test_official_catalog_matches_endpoint_and_alias_exactly(self):
        for base in ("https://api.deepseek.com", "https://api.deepseek.com/v1/chat/completions"):
            self.assertEqual(load_capability(base, "deepseek-v4-flash")["context_window_tokens"], 1048576)
        self.assertEqual(load_capability("https://proxy.test/v1", "deepseek-v4-flash"), {})
        self.assertEqual(load_capability("https://api.deepseek.com", "unknown"), {})

    def test_known_model_needs_no_network_and_sends_output_limit(self):
        client = ChatCompletionsClient(Config(base_url="https://api.deepseek.com", api_key="", model="deepseek-v4-flash"))
        with patch("jarvis_agent.urllib.request.urlopen") as network:
            config = client.resolve_config()
        network.assert_not_called()
        self.assertEqual(config.max_output_tokens, 32768)
        response = {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 10}}
        with patch("jarvis_agent.urllib.request.urlopen", return_value=FakeHTTPResponse(response)) as network:
            client.complete([{"role": "user", "content": "hello"}], [])
        self.assertEqual(json.loads(network.call_args.args[0].data)["max_tokens"], 32768)

    def test_list_discovery_matches_model_and_ignores_input_limit(self):
        client = ChatCompletionsClient(self.config())
        payloads = [FakeHTTPResponse({"max_input_tokens": 90000}), FakeHTTPResponse({"data": [
            {"id": "other", "context_window": 90000}, {"id": "test", "context_window": 20000}]})]
        with patch("jarvis_agent.urllib.request.urlopen", side_effect=payloads):
            self.assertEqual(client.discover_context_window(), 20000)
        with patch("jarvis_agent.urllib.request.urlopen", return_value=FakeHTTPResponse({"data": [{"id": "other", "context_window": 90000}]})):
            self.assertIsNone(client.discover_context_window())

    def test_unknown_capacity_requires_configuration(self):
        with patch.object(ChatCompletionsClient, "discover_context_window", return_value=None):
            with self.assertRaises(ConfigurationError):
                ChatCompletionsClient(self.config()).resolve_config()

    def test_user_catalog_and_explicit_window_override_and_output_clamp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            path.write_text(json.dumps({"models": [{"base_url": "https://example.test", "model_ids": ["test"],
                "context_window_tokens": 20000, "max_output_tokens": 500, "source": "test", "checked_at": "2026-09-11"}]}), encoding="utf-8")
            client = ChatCompletionsClient(self.config(model_capabilities_file=path, context_window_tokens=30000))
            with patch("jarvis_agent.urllib.request.urlopen") as network:
                config = client.resolve_config()
            network.assert_not_called()
            self.assertEqual(config.context_window_tokens, 30000)
            self.assertEqual(config.max_output_tokens, 500)

    def test_compression_model_does_not_inherit_main_capacity(self):
        config = self.config(context_window_tokens=50000, compression_model="other",
                             compression_context_window_tokens=20000, compression_max_output_tokens=500)
        with redirect_stdout(StringIO()):
            agent = make_agent(self, config)
        self.assertEqual(agent.compression_client.client.config.context_window_tokens, 20000)
        self.assertEqual(agent.compression_client.client.config.max_output_tokens, 500)

    def test_impossible_reserves_fail_before_request(self):
        with self.assertRaises(ConfigurationError):
            ChatCompletionsClient(self.config(context_window_tokens=1000)).resolve_config()

    def test_budget_guard_stops_oversized_request(self):
        client = ChatCompletionsClient(self.config(context_window_tokens=20000))
        client.resolve_config()
        with patch("jarvis_agent.urllib.request.urlopen") as network:
            with self.assertRaises(ModelRequestError):
                client.complete([{"role": "user", "content": "x" * 100000}], [])
        network.assert_not_called()

    def test_threshold_strictly_greater_than_ninety_percent_with_reserves(self):
        manager = ContextManager(self.config(context_window_tokens=10000))
        # output 1000 + margin 200; equality does not trigger.
        self.assertFalse(manager.should_compress(7800))
        self.assertTrue(manager.should_compress(7801))
        self.assertEqual(manager.input_budget(), 8800)

    def test_usage_anchor_includes_cache_and_resets_after_history_changes(self):
        manager = ContextManager(self.config(context_window_tokens=10000))
        base = [{"role": "system", "content": "stable"}, {"role": "user", "content": "hello"}]
        old = [*base, {"role": "user", "content": "status"}]
        manager.record_usage({"prompt_tokens": 2000, "prompt_cache_hit_tokens": 1800}, old, [])
        new = [*base, {"role": "assistant", "content": "answer"}, {"role": "user", "content": "next status"}]
        self.assertEqual(manager._estimate_request(new, []), 2000 + estimate_tokens(new, []) - estimate_tokens(old, []))
        changed = [{"role": "system", "content": "changed"}, *new[1:]]
        self.assertEqual(manager._estimate_request(changed, []), estimate_tokens(changed, []))
        manager.record_usage(None)
        self.assertEqual(manager._estimate_request(new, []), estimate_tokens(new, []))

    def test_task_metrics_reset_and_reasoning_history_preserved(self):
        raw = FakeClient([{"content": "a", "reasoning_content": "reason"}, {"content": "b"}])
        agent = make_agent(self, self.config(), raw)
        with redirect_stdout(StringIO()):
            agent.run_request("a")
            agent.run_request("b")
        self.assertEqual(len(agent.usage_ledger.records), 1)
        self.assertEqual(agent.messages[2]["reasoning_content"], "reason")

    def test_compression_uses_input_target_and_records_compression_usage(self):
        manager = ContextManager(self.config(context_window_tokens=10000, context_summary_max_chars=100))
        manager.begin_task("find facts")
        messages = [{"role": "system", "content": "stable"}, {"role": "user", "content": "find facts"}]
        for index in range(2):
            call_id = str(index)
            messages.extend([
                {"role": "assistant", "tool_calls": [{"id": call_id, "function": {"name": "read_file", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": call_id, "content": "x" * 16000},
            ])
        class Compressor:
            last_usage = None
            def complete(self, *args):
                self.last_usage = {"prompt_tokens": 4200, "prompt_cache_hit_tokens": 0}
                return {"content": "<context_summary>source.txt:1 fact</context_summary>"}
        ledger = UsageLedger()
        with redirect_stdout(StringIO()):
            prepared = manager.prepare_messages(messages, [], MeasuredClient(Compressor(), ledger, "压缩模型", "test"))
        self.assertLessEqual(estimate_tokens(prepared, []), manager.input_budget() * .8)
        self.assertFalse(manager.last_metrics["over_budget"])
        self.assertEqual(len(ledger.records), 1)
        self.assertEqual(ledger.records[0]["role"], "压缩模型")
        self.assertIsNone(manager.last_usage_tokens)

    def test_uncompressible_history_is_rejected_before_main_call(self):
        raw = FakeClient([])
        agent = make_agent(self, self.config(context_window_tokens=10000), raw)
        with redirect_stdout(StringIO()):
            self.assertIsNone(agent.run_request("x" * 50000))
        self.assertEqual(raw.calls, [])
        self.assertEqual(len(agent.messages), 1)


if __name__ == "__main__":
    unittest.main()
