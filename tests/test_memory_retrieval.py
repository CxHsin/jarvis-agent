import json

import pytest

from jarvis_agent import Agent, Config
from memory_service import MemoryService
from stable_memory import timestamp


sqlite_vec = pytest.importorskip("sqlite_vec")


class FakeEmbedding:
    def embed(self, text, model=None):
        return [1.0, 0.0, 0.0]


class FakeRewrite:
    def __init__(self):
        self.calls = []

    def complete(self, messages, tools, tool_choice):
        self.calls.append(messages)
        if messages[0]["role"] == "system":
            return {"content": "tea preference"}
        return {"content": json.dumps({"query": "tea"})}


def source():
    return {
        "quote": "I prefer tea",
        "recorded_at": timestamp(),
        "source_task_id": "task-1",
        "source_event_id": "event-1",
        "trajectory_path": "trajectory.jsonl",
    }


def test_memory_search_uses_real_fts_vec_and_ephemeral_hyde(tmp_path):
    rewrite = FakeRewrite()
    memory = MemoryService(tmp_path, embedding_client=FakeEmbedding(), rewrite_client=rewrite,
                           embedding_dimensions=3)
    fact_id = memory.remember({"subject": "USER", "predicate": "likes", "object": "tea",
                               "text": "Tea is my preferred drink", "category": "work_preferences"},
                              source=source())

    result = memory.search("What drink do I prefer?")

    assert result["vector_available"] is True
    assert result["facts"][0]["fact_id"] == fact_id
    assert result["facts"][0]["sources"][0]["source_event_id"] == "event-1"
    assert not memory.facts(include_inactive=True)[0].get("candidate_text")
    assert any(call[0]["role"] == "system" for call in rewrite.calls)
    memory.close()


def test_memory_tools_are_reachable_through_agent_runtime(tmp_path):
    class Client:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools, tool_choice):
            self.calls += 1
            if self.calls == 1:
                return {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "search-tools", "type": "function",
                    "function": {"name": "tool_search", "arguments": json.dumps({"query": "memory"})},
                }]}
            if self.calls == 2:
                return {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "search-memory", "type": "function",
                    "function": {"name": "memory_search", "arguments": json.dumps({"query": "tea"})},
                }]}
            return {"role": "assistant", "content": "done"}

    config = Config(base_url="http://example.test", api_key="", model="test",
                    root_dir=tmp_path, state_dir=tmp_path / "state", max_rounds=4,
                    embedding_dimensions=3)
    agent = Agent(config, Client(), embedding_client=FakeEmbedding())
    try:
        agent.store.memory.remember({"subject": "USER", "predicate": "likes", "object": "tea",
                                     "text": "tea", "category": "work_preferences"}, source=source())
        assert agent.run_request("find tea") == "done"
        tool_messages = [m for m in agent.messages if m.get("role") == "tool"]
        assert any(json.loads(m["content"]).get("facts") for m in tool_messages)
    finally:
        agent.close()
