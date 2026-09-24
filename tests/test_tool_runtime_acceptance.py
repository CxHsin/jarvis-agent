"""Acceptance regressions for #17, exercised through runtime and Agent seams."""
import json
import hashlib
import subprocess
import sys
import time
from threading import Barrier, Event
from copy import deepcopy

import pytest

from agent.agent import Agent
from configuration import Config
from tools.workspace import Workspace
from unittest.mock import patch
from tools.tool_runtime import (ToolMetadata, ToolRegistry, ToolRuntime, ToolDispatcher,
                          PermissionPolicy, ProviderLoadError)
from models.model_client import ModelRequestError


class Client:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, messages, tools, tool_choice):
        self.requests.append(deepcopy((messages, tools, tool_choice)))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response() if callable(response) else response


def call(name, arguments=None, call_id="c1", **extra):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments or {})}, **extra}


def answer(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


def config(tmp_path, **kwargs):
    return Config(base_url="http://example.test", api_key="", model="test",
                  root_dir=tmp_path, state_dir=tmp_path / "state", **kwargs)


def tool_results(agent):
    return [json.loads(m["content"]) for m in agent.messages if m["role"] == "tool"]


def test_registered_schema_cannot_change_through_returned_metadata():
    registry = ToolRegistry()
    metadata = ToolMetadata("echo", "1", {"name": "echo", "parameters": {
        "type": "object", "properties": {"text": {"type": "string"}}}})
    registry.register(metadata, lambda **kw: kw)
    fingerprint = metadata.schema_fingerprint
    metadata.schema["parameters"]["properties"]["text"]["type"] = "integer"
    assert registry.get("echo", "1").metadata.schema["parameters"]["properties"]["text"]["type"] == "string"
    assert metadata.schema_fingerprint == fingerprint


def test_runtime_discovery_activates_dynamic_tool_only_for_current_task():
    runtime = ToolRuntime(ToolRegistry())
    seen = []
    runtime.register(ToolMetadata("echo", "1", {"name": "echo", "parameters": {
        "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}),
        lambda text: seen.append(text) or {"ok": True, "text": text})
    runtime.begin_task("first")
    assert [item["tool_id"] for item in runtime.discover("first", "echo")["tools"]] == ["echo"]
    assert runtime.execute_model_call("first", "echo", {"text": "found"}) == {"ok": True, "text": "found"}
    runtime.end_task("first")
    runtime.begin_task("second")
    assert seen == ["found"]
    assert runtime.execute_model_call("second", "echo", {"text": "stale"})["error"]["code"] == "inactive_tool"
    assert ("echo", "1") in runtime.history


def test_agent_validates_argument_types_before_edit(tmp_path):
    client = Client(answer(call("edit", {"path": "test.txt", "content": 123})), {"content": "done"})
    agent = Agent(config(tmp_path), client)
    try:
        agent.run_request("edit")
        assert not (tmp_path / "test.txt").exists()
        assert tool_results(agent)[0]["error"]["code"] == "invalid_arguments"
    finally:
        agent.close()


def register(agent, name, handler, *, parameters=None, **metadata):
    return agent.tool_registry.register(ToolMetadata(name, "1", {"name": name, "parameters": parameters or {
        "type": "object", "additionalProperties": False}}, **metadata), handler)


def test_registry_rejects_rebinding_handler_and_invalid_metadata():
    registry = ToolRegistry()
    schema = {"name": "x", "parameters": {"type": "object"}}
    metadata = ToolMetadata("x", "1", schema)
    handler = lambda: "first"
    registry.register(metadata, handler)
    schema["parameters"]["type"] = "string"
    assert metadata.schema["parameters"]["type"] == "object"
    assert registry.register(metadata, handler).handler is handler
    with pytest.raises(ValueError, match="inconsistent"):
        registry.register(metadata, lambda: "changed")
    with pytest.raises(ValueError):
        ToolMetadata("different", "1", metadata.schema)
    with pytest.raises(ValueError, match="local"):
        ToolMetadata("x", "1", {"name": "x", "parameters": {"type": "object", "$ref": "https://example.test/schema"}})


@pytest.mark.parametrize("legacy_mode", ["approve-all", "approve-dangerous", "broad-access"])
def test_legacy_permission_modes_cannot_gate_workspace_shell(tmp_path, legacy_mode):
    client = Client(answer(call("bash", {"command": "echo safe"})), {"content": "done"})
    agent = Agent(config(tmp_path, tool_permission_mode=legacy_mode), client,
                  confirm_tool=lambda metadata, args: pytest.fail("legacy confirmation called"))
    try:
        with patch.object(Workspace, "bash", return_value={"ok": True}) as shell:
            agent.run_request("run")
            shell.assert_called_once()
        assert tool_results(agent)[0]["ok"]
        assert any(item.get("phase") == "finished" for item in agent.store.load().audit)
    finally:
        agent.close()


def test_revocation_survives_resume_without_legacy_mode_grants(tmp_path):
    agent = Agent(config(tmp_path), Client(answer(call("write", {"path": "new.txt", "content": "text"})),
                                          {"content": "done"}))
    session = agent.store.session_id
    try:
        agent.tool_runtime.policy.revoke()
        agent.run_request("write")
        assert tool_results(agent)[0]["error"]["code"] == "policy_revoked"
        assert not (tmp_path / "new.txt").exists()
        assert any(e.get("action") == "revoked" for e in agent.store.load().audit)
    finally:
        agent.close()
    resumed = Agent(config(tmp_path, tool_permission_mode="broad-access"), Client({"content": "done"}), resume=session)
    try:
        assert resumed.tool_runtime.policy.revoked
        assert resumed.tool_runtime.policy.version == 2
    finally:
        resumed.close()


def test_revocation_blocks_write_before_execution(tmp_path):
    policy = PermissionPolicy()
    agent = Agent(config(tmp_path), Client(answer(call("write", {"path": "x.txt", "content": "x"})),
                                          {"content": "done"}), permission_policy=policy)
    try:
        policy.revoke()
        agent.run_request("write")
        assert not (tmp_path / "x.txt").exists()
        assert tool_results(agent)[0]["error"]["code"] == "policy_revoked"
    finally:
        agent.close()


def test_search_filters_denied_tools_and_keeps_historical_definition():
    policy = PermissionPolicy(denied_tools=("hidden",))
    runtime = ToolRuntime(ToolRegistry(), policy=policy)
    for name in ("visible", "hidden"):
        runtime.register(ToolMetadata(name, "1", {"name": name, "parameters": {"type": "object"}}), lambda: {"ok": True})
    runtime.begin_task("first")
    definitions = runtime.discover("first")["tools"]
    assert [d["tool_id"] for d in definitions] == ["visible"]
    history = deepcopy(runtime.history)
    runtime.end_task("first")
    restored = ToolRuntime(ToolRegistry(), policy=policy)
    restored.register(ToolMetadata("visible", "1", definitions[0]["schema"]), lambda: pytest.fail("reactivated"))
    restored.restore({"definitions": [dict(d) for d in history.values()]})
    restored.begin_task("next")
    assert restored.execute_model_call("next", "visible", {})["error"]["code"] == "inactive_tool"
    assert ("visible", "1") in restored.history


def test_runtime_executes_independent_tools_concurrently():
    barrier = Barrier(2)
    def work():
        barrier.wait(timeout=2)
        return {"ok": True}
    runtime = ToolRuntime(ToolRegistry(), stable=(("parallel_a", "1"), ("parallel_b", "1")))
    for name in ("parallel_a", "parallel_b"):
        runtime.register(ToolMetadata(name, "1", {"name": name, "parameters": {"type": "object"}},
                                      concurrency="parallel"), work)
    runtime.begin_task("parallel")
    results = []
    runtime.execute_batch("parallel", [call("parallel_a", call_id="a"), call("parallel_b", call_id="b")],
                          lambda raw, result: results.append((raw.call_id, result)))
    assert [cid for cid, _ in results] == ["a", "b"]
    assert all(item["ok"] for _, item in results)


def test_same_file_edits_detect_stale_second_write(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("original")
    client = Client(answer(call("edit", {"path": "note.txt", "content": "first"}, "a"),
                           call("edit", {"path": "./note.txt", "content": "second"}, "b")), {"content": "done"})
    agent = Agent(config(tmp_path), client)
    try:
        agent.run_request("edit")
        assert target.read_text() == "first"
        assert tool_results(agent)[1]["error"]["code"] == "edit_conflict"
    finally:
        agent.close()


def test_dependencies_resolve_results_before_validation_and_write(tmp_path):
    client = Client(answer(call("edit", {"path": "note.txt", "content": "first"}, "a"),
                           call("edit", {"path": "note.txt", "content": "second", "_depends_on": ["a"],
                                         "expected_hash": {"$result": {"call_id": "a", "path": ["hash"]}}}, "b")),
                    {"content": "done"})
    agent = Agent(config(tmp_path), client)
    try:
        agent.run_request("dependent edits")
        assert (tmp_path / "note.txt").read_text() == "second"
        assert all(item["ok"] for item in tool_results(agent))
    finally:
        agent.close()


@pytest.mark.parametrize("extra,code", [
    ({"_depends_on": ["a"]}, "dependency_cycle"),
    ({"_depends_on": ["absent"]}, "missing_dependency"),
    ({"_version": "missing"}, "unavailable_version"),
    ({"_schema_fingerprint": "wrong"}, "schema_mismatch"),
])
def test_invalid_call_contract_blocks_effects(tmp_path, extra, code):
    agent = Agent(config(tmp_path), Client(answer(call("edit", {"path": "x.txt", "content": "x", **extra}, "a")),
                                          {"content": "done"}))
    try:
        agent.run_request("write")
        assert not (tmp_path / "x.txt").exists()
        assert tool_results(agent)[0]["error"]["code"] == code
    finally:
        agent.close()


def test_failed_dependency_does_not_run_following_effect(tmp_path):
    client = Client(answer(call("edit", {"path": "x.txt", "content": "a", "expected_hash": "stale"}, "a"),
                           call("edit", {"path": "y.txt", "content": "b", "_depends_on": ["a"]}, "b")), {"content": "done"})
    agent = Agent(config(tmp_path), client)
    try:
        agent.run_request("write")
        assert not (tmp_path / "y.txt").exists()
        assert tool_results(agent)[1]["error"]["code"] == "dependency_failed"
    finally:
        agent.close()


def test_timeout_retains_lease_until_uncooperative_handler_finishes():
    registry, release, finished = ToolRegistry(), Event(), Event()
    def slow():
        release.wait(3)
        finished.set()
        return "done"
    metadata = ToolMetadata("slow", "1", {"name": "slow", "parameters": {"type": "object"}},
                            timeout=0.03, side_effects=("write",), resources=("file",), concurrency="parallel")
    registry.register(metadata, slow)
    dispatcher = ToolDispatcher(registry)
    try:
        result = dispatcher.execute("slow", "1")
        assert result["error"]["code"] == "timeout"
        assert result["uncertain"]
        assert dispatcher.execute("slow", "1")["error"]["code"] == "resource_conflict"
    finally:
        release.set()
        assert finished.wait(1)


@pytest.mark.parametrize("value", ["x" * 4000, '"\\' * 4000, {"ok": False, "error": "失败" * 4000}])
def test_output_limit_is_enforced(value):
    registry = ToolRegistry()
    registry.register(ToolMetadata("large", "1", {"name": "large", "parameters": {"type": "object"}},
                                    output_limit=512), lambda: value)
    result = ToolDispatcher(registry).execute("large", "1")
    payload = {k: v for k, v in result.items() if k != "audit"}
    assert (payload.get("result") or payload)["truncated"]
    assert len(json.dumps(payload, ensure_ascii=False).encode()) <= 512


def test_agent_native_fallback_is_persistent_and_discards_partial_changes(tmp_path):
    attempts = []
    def native(client, messages, tools, dynamic, choice):
        attempts.append(True)
        messages.append({"role": "user", "content": "partial native references"})
        raise ProviderLoadError("network unavailable")
    client = Client({"content": "done"})
    cfg = config(tmp_path, provider_tool_mode="native")
    agent = Agent(cfg, client, native_loader=native)
    session = agent.store.session_id
    try:
        agent.run_request("hello")
        assert len(attempts) == 3
        assert len(client.requests) == 1
        assert "partial native references" not in json.dumps(client.requests)
        assert agent.provider_session.fallback_count == 1
    finally:
        agent.close()
    resumed = Agent(cfg, Client({"content": "done"}), resume=session,
                    native_loader=lambda *args: pytest.fail("fallback was not persisted"))
    try:
        resumed.run_request("next")
        assert resumed.provider_session.fallback_count == 1
        assert resumed.provider_session.mode == "emulated"
    finally:
        resumed.close()


def test_agent_native_adapter_receives_only_four_public_definitions(tmp_path):
    received = []
    def native(client, messages, tools, dynamic, choice):
        received.append((tools, dynamic))
        return client.complete(messages, tools, choice)
    agent = Agent(config(tmp_path, provider_tool_mode="native"), Client({"content": "done"}), native_loader=native)
    register(agent, "native_tool", lambda: "value")
    try:
        agent.run_request("discover")
        assert [item["function"]["name"] for item in received[0][0]] == ["read", "write", "edit", "bash"]
        assert all(not dynamic for _, dynamic in received)
        assert agent.provider_session.mode == "native"
    finally:
        agent.close()


def test_model_failure_after_effect_preserves_audit_and_does_not_replay(tmp_path):
    client = Client(answer(call("edit", {"path": "note.txt", "content": "written"})), ModelRequestError("network"))
    cfg = config(tmp_path)
    agent = Agent(cfg, client)
    session = agent.store.session_id
    try:
        assert agent.run_request("write") is None
        assert (tmp_path / "note.txt").read_text() == "written"
        assert tool_results(agent)[0]["ok"]
        assert any(e.get("phase") == "finished" for e in agent.store.load().audit)
    finally:
        agent.close()
    resumed = Agent(cfg, Client({"content": "done"}), resume=session)
    try:
        assert len(tool_results(resumed)) == 1
        resumed.run_request("continue")
        assert (tmp_path / "note.txt").read_text() == "written"
    finally:
        resumed.close()


def test_compaction_preserves_four_tool_prefix(tmp_path):
    def compact_and_call():
        assert agent.compact_now(keep_tokens=1).compacted
        return answer(call("read", {"path": "note.md"}, "c3"))
    (tmp_path / "note.md").write_text("after compaction", encoding="utf-8")
    client = Client(answer(call("read", {"path": "note.md"}, "c2")), compact_and_call, {"content": "done"})
    compressor = Client({"content": "<context_summary>Read note; tool result archived.</context_summary>"})
    agent = Agent(config(tmp_path), client, compression_client=compressor)
    fingerprint = agent.tool_runtime.stable_fingerprint()
    try:
        agent.run_request("read twice")
        assert tool_results(agent)[-1]["ok"]
        assert agent.tool_runtime.stable_fingerprint() == fingerprint
        assert all(request[1] == client.requests[0][1] for request in client.requests)
        assert [tool["function"]["name"] for tool in client.requests[0][1]] == ["read", "write", "edit", "bash"]
        for request, _, _ in client.requests:
            calls = {c["id"] for m in request for c in m.get("tool_calls", [])}
            replies = {m["tool_call_id"] for m in request if m["role"] == "tool"}
            assert calls == replies
        assert any(e.get("type") == "compact" for e in map(json.loads, agent.store.path.read_text(encoding="utf-8").splitlines()))
    finally:
        agent.close()


def test_runtime_rejects_changed_schema_under_historical_version():
    runtime = ToolRuntime(ToolRegistry())
    runtime.register(ToolMetadata("dynamic", "1", {"name": "dynamic", "parameters": {"type": "object"}}), lambda: True)
    runtime.begin_task("old")
    runtime.discover("old", "dynamic")
    snapshot = runtime.snapshot()
    restored = ToolRuntime(ToolRegistry())
    restored.restore(snapshot)
    restored.register(ToolMetadata("dynamic", "1", {"name": "dynamic", "parameters": {
        "type": "object", "properties": {"new": {"type": "string"}}}}), lambda **kw: pytest.fail("executed"))
    restored.begin_task("new")
    restored.discover("new", "dynamic")
    assert restored.execute_model_call("new", "dynamic", {})["error"]["code"] == "schema_mismatch"


def test_fallback_survives_model_failure_and_request_rollback(tmp_path):
    def native(*args):
        raise ProviderLoadError("unsupported")
    agent = Agent(config(tmp_path, provider_tool_mode="native"), Client(ModelRequestError("failed")), native_loader=native)
    try:
        assert agent.run_request("fail") is None
        contents = agent.store.load()
        assert contents.messages == []
        assert contents.runtime["provider"]["mode"] == "emulated"
        assert contents.runtime["provider"]["fallback_count"] == 1
    finally:
        agent.close()


def test_cancelled_batch_keeps_tool_pairing_and_audits(tmp_path):
    cancelled = Event()
    def stop(*args, **kwargs):
        cancelled.set()
        raise KeyboardInterrupt
    client = Client(answer(call("read", {"path": "first.txt"}, call_id="a"),
                           call("read", {"path": "next.txt"}, call_id="b")))
    with patch.object(Workspace, "read", side_effect=stop):
        agent = Agent(config(tmp_path), client)
        session_id = agent.store.session_id
        try:
            assert agent.run_request("cancel") is None
            assert cancelled.is_set()
            assert all(result["cancelled"] for result in tool_results(agent))
            calls = {c["id"] for m in agent.messages for c in m.get("tool_calls", [])}
            replies = {m["tool_call_id"] for m in agent.messages if m["role"] == "tool"}
            assert calls == replies
            assert any(e.get("error", {}).get("code") == "cancelled" for e in agent.store.load().audit
                       if isinstance(e.get("error"), dict))
        finally:
            agent.close()
    resumed = Agent(config(tmp_path), Client(), resume=session_id)
    try:
        restored_calls = {c["id"] for m in resumed.messages for c in m.get("tool_calls", [])}
        restored_replies = {m["tool_call_id"] for m in resumed.messages if m["role"] == "tool"}
        assert restored_calls == restored_replies == calls
        assert all(result["cancelled"] for result in tool_results(resumed))
    finally:
        resumed.close()


def test_session_revocation_cannot_be_widened_by_restart_config(tmp_path):
    agent = Agent(config(tmp_path), Client({"content": "done"}))
    session = agent.store.session_id
    try:
        agent.tool_runtime.policy.revoke()
        agent.run_request("hello")
    finally:
        agent.close()
    resumed = Agent(config(tmp_path, tool_permission_mode="broad-access"), Client(), resume=session)
    try:
        assert resumed.tool_runtime.policy.revoked
    finally:
        resumed.close()


def test_builtin_shell_timeout_stops_process_before_delayed_write(tmp_path):
    script = tmp_path / "delayed.py"
    marker = tmp_path / "late.txt"
    script.write_text("import time\nfrom pathlib import Path\ntime.sleep(0.5)\nPath('late.txt').write_text('late')\n", encoding="utf-8")
    # cmd.exe in AppContainer needs an explicitly quoted executable path,
    # even without spaces; list2cmdline would leave that path unquoted.
    command = f'"{sys.executable}" ' + subprocess.list2cmdline([str(script)])
    agent = Agent(config(tmp_path, tool_permission_mode="broad-access"),
                  Client(answer(call("bash", {"command": command, "timeout": 0.1})), {"content": "done"}))
    try:
        agent.run_request("timeout")
        assert tool_results(agent)[0]["error"]["code"] == "timeout"
        time.sleep(0.6)
        assert not marker.exists()
    finally:
        agent.close()


def test_read_hash_matches_exact_content_and_prevents_stale_edit(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("first", encoding="utf-8")
    workspace = Workspace(config(tmp_path))
    result = workspace.read("note.txt")
    assert result["hash"] == hashlib.sha256(b"first").hexdigest()
    target.write_text("external update", encoding="utf-8")
    outcome = workspace.edit("note.txt", "stale replacement", expected_hash=result["hash"])
    assert outcome["error"]["code"] == "edit_conflict"
    assert target.read_text() == "external update"


def test_revoked_write_does_not_start_workers(tmp_path):
    agent = Agent(config(tmp_path), Client(answer(call("write", {"path": "x.txt", "content": "x"})),
                                          {"content": "done"}))
    try:
        agent.tool_runtime.policy.revoke()
        assert agent.run_request("write") == "done"
        assert tool_results(agent)[0]["error"]["code"] == "policy_revoked"
        assert not (tmp_path / "x.txt").exists()
        assert not any(e.get("phase") == "started" for e in agent.store.load().audit)
    finally:
        agent.close()


def test_failed_request_after_denied_write_preserves_revocation_audit(tmp_path):
    policy = PermissionPolicy()
    agent = Agent(config(tmp_path),
                  Client(answer(call("write", {"path": "x.txt", "content": "x"})), ModelRequestError("failed")),
                  permission_policy=policy)
    try:
        policy.revoke()
        assert agent.run_request("write") is None
        contents = agent.store.load()
        assert contents.messages == []
        assert any(e.get("error", {}).get("code") == "policy_revoked" for e in contents.audit
                   if isinstance(e.get("error"), dict))
        assert contents.runtime["policy"]["revoked"]
        assert any(e.get("action") == "revoked" for e in contents.audit)
        assert not (tmp_path / "x.txt").exists()
    finally:
        agent.close()


def test_runtime_preserves_injected_confirmation():
    registry = ToolRegistry()
    seen = []
    registry.register(ToolMetadata("host_write", "1", {"name": "host_write", "parameters": {
        "type": "object"}}, side_effects=("host",)), lambda: seen.append("write") or "saved")
    runtime = ToolRuntime(registry, stable=(("host_write", "1"),), confirm=lambda metadata, args: True)
    runtime.begin_task("host")
    assert runtime.execute_model_call("host", "host_write", {}) == {"ok": True, "value": "saved"}
    assert seen == ["write"]
    assert any(e.get("phase") == "finished" for e in runtime.dispatcher.audit)


def test_runtime_preserves_host_resource_resolution():
    registry = ToolRegistry()
    seen = []
    registry.register(ToolMetadata("host", "1", {"name": "host", "parameters": {
        "type": "object"}}, concurrency="parallel"), lambda: seen.append("executed"))
    def resources(metadata, arguments):
        raise ValueError("host resource unavailable")
    runtime = ToolRuntime(registry, stable=(("host", "1"),), resource_resolver=resources)
    runtime.begin_task("host")
    result = runtime.execute_model_call("host", "host", {})
    assert seen == []
    assert result["error"]["code"] == "invalid_arguments"
    assert "host resource unavailable" in result["error"]["message"]


def test_runtime_timeout_keeps_host_resource_and_unknown_audit():
    release, finished = Event(), Event()
    seen = []
    def slow():
        try:
            release.wait(5)
            seen.append("slow")
            return "late"
        finally:
            finished.set()
    runtime = ToolRuntime(ToolRegistry(), stable=(("slow", "1"), ("next", "1")))
    for name, handler in (("slow", slow), ("next", lambda: seen.append("next"))):
        runtime.register(ToolMetadata(name, "1", {"name": name, "parameters": {"type": "object"}},
                         timeout=0.03, resources=("host-file",), side_effects=("write",),
                         concurrency="parallel"), handler)
    runtime.begin_task("run")
    try:
        first = runtime.execute_model_call("run", "slow", {}, call_id="slow")
        second = runtime.execute_model_call("run", "next", {}, call_id="next")
        assert first["error"]["code"] == "timeout"
        assert first["uncertain"] is True
        assert second["error"]["code"] == "resource_conflict"
        assert seen == []
        assert any(e.get("call_id") == "slow" and e.get("uncertain") for e in runtime.dispatcher.audit)
    finally:
        release.set()
        assert finished.wait(2)
    assert seen == ["slow"]


def test_runtime_records_parallel_results_and_references_deterministically():
    second_finished = Event()
    def first():
        assert second_finished.wait(2)
        return "first"
    def second():
        second_finished.set()
        return {"text": "second"}
    runtime = ToolRuntime(ToolRegistry(), stable=(("first", "1"), ("second", "1"), ("join", "1")))
    for name, handler, parameters in (
        ("first", first, {"type": "object"}),
        ("second", second, {"type": "object"}),
        ("join", lambda text: {"text": text}, {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}),
    ):
        runtime.register(ToolMetadata(name, "1", {"name": name, "parameters": parameters},
                                      concurrency="parallel"), handler)
    runtime.begin_task("parallel")
    results = []
    runtime.execute_batch("parallel", [call("first", call_id="a"), call("second", call_id="b"),
        call("join", {"text": {"$result": {"call_id": "a", "path": ["value"]}}}, "c")],
        lambda item, result: results.append((item.call_id, result)))
    assert results == [("a", {"ok": True, "value": "first"}), ("b", {"text": "second"}),
                       ("c", {"text": "first"})]
