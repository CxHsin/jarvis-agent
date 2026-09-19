import json
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO

import pytest

from agent.agent import Agent
from configuration import Config
from tests.test_jarvis_agent import FakeClient


@pytest.mark.parametrize("tool", ["read", "read_file"])
@pytest.mark.parametrize("truncated", [False, True])
def test_read_evidence_survives_compaction_and_resume(tmp_path, tool, truncated):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "note.md").write_text("first\nsecond\nthird\n", encoding="utf-8")
    config = Config(
        base_url="http://example.test/v1", api_key="", model="test",
        root_dir=root, state_dir=tmp_path / "state",
        max_read_chars=10 if truncated else 1000,
    )

    def call(call_id, path):
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": tool, "arguments": json.dumps({
                "path": path, "start_line": 2, "end_line": 3,
            })},
        }]}

    client = FakeClient([
        call("first", "note.md"), call("repeat", "note.md"),
        call("missing", "missing.md"),
        {"role": "assistant", "content": "read complete"},
        {"role": "assistant", "content": "next task"},
    ])
    compressor = FakeClient([{"content": "<context_summary>Sources: E1</context_summary>"}])
    with redirect_stdout(StringIO()):
        agent = Agent(config, client, compression_client=compressor)
        try:
            assert agent.run_request("Read lines 2-3 twice and try the missing file") == "read complete"
            results = [json.loads(m["content"]) for m in client.requests[3][0] if m["role"] == "tool"]
            assert results[0]["content"] == ("2: second" if truncated else "2: second\n3: third")
            assert results[0]["truncated"] is truncated
            if truncated:
                assert results[0]["next_start_line"] == 3
            assert results[1] == results[0]
            assert results[2]["ok"] is False
            expected_ref = "note.md:2-2" if truncated else "note.md:2-3"
            assert agent.context.session_evidence == [{
                "id": "E1", "task": 1, "tool": tool, "refs": [expected_ref],
                "repeated": True, "compressed": False,
            }]
            assert [entry["evidence_id"] for entry in agent.context.session_archive] == ["E1", "E1", None]
            agent.run_request("Start the next task")
            assert agent.compact_now().compacted
            assert expected_ref in compressor.requests[0][0][-1]["content"]
            assert agent.context.session_evidence[0]["compressed"] is True
            evidence = deepcopy(agent.context.session_evidence)
            archive = deepcopy(agent.context.session_archive)
            session_id = agent.store.session_id
        finally:
            agent.close()

        resumed_client = FakeClient([{"role": "assistant", "content": "Sources: E1"}])
        resumed = Agent(config, resumed_client, resume=session_id)
        try:
            assert resumed.context.session_evidence == evidence
            assert resumed.context.session_archive == archive
            assert resumed.context.session_archive[0]["result"] == results[0]
            assert resumed.run_request("Recall the source") == "Sources: E1"
            restored_results = [json.loads(m["content"]) for m in resumed_client.requests[0][0] if m["role"] == "tool"]
            assert results[0] not in restored_results
            assert "Sources: E1" in str(resumed_client.requests[0][0])
            assert resumed.context.session_evidence[0]["refs"] == [expected_ref]
        finally:
            resumed.close()
