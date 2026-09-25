"""Issue #47: four-tool boundary and cooperative cancellation."""
import json
import os
from threading import Event

import pytest

from agent.agent import Agent
from configuration import Config
from tools.definitions import TOOL_DEFINITIONS
from tools.workspace import Workspace, WorkspaceError
from tools.tool_runtime import PermissionPolicy


def config(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return Config(base_url="http://example.test", api_key="", model="test",
                  root_dir=root, state_dir=tmp_path / "state")


def test_four_schemas_and_legacy_grant_ignored(tmp_path):
    assert [d["function"]["name"] for d in TOOL_DEFINITIONS] == ["read", "write", "edit", "bash"]
    policy = PermissionPolicy()
    policy.restore({"mode": "broad-access", "denied_tools": ["bash"]})
    assert not policy.check(type("Metadata", (), {"tool_id": "bash", "side_effects": ("filesystem",)})())[0]
    assert "mode" not in policy.snapshot()


def test_workspace_read_write_edit_and_hash_conflict(tmp_path):
    workspace = Workspace(config(tmp_path))
    created = workspace.write("new.txt", "first\nsecond")
    assert created["ok"] and workspace.read("new.txt")["content"] == "1: first\n2: second"
    conflict = workspace.edit("new.txt", "replaced", start_line=2, expected_hash="stale")
    assert conflict["error"]["code"] == "edit_conflict"
    updated = workspace.edit("new.txt", "changed", start_line=2, expected_hash=created["hash"])
    assert updated["ok"] and workspace.read("new.txt")["content"] == "1: first\n2: changed"
    assert workspace.write("new.txt", "overwritten", expected_hash=updated["hash"])["ok"]
    assert workspace.read("new.txt")["content"] == "1: overwritten"


@pytest.mark.parametrize("path", ["../outside.txt", "outside.txt", "../state/log.txt"])
def test_outside_file_tools_denied(tmp_path, path):
    workspace = Workspace(config(tmp_path))
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    if path == "outside.txt":
        path = str(tmp_path / "outside.txt")
    for action in (lambda: workspace.read(path), lambda: workspace.write(path, "bad"),
                   lambda: workspace.edit(path, "bad")):
        with pytest.raises(WorkspaceError):
            action()
    assert (tmp_path / "outside.txt").read_text(encoding="utf-8") == "secret"


def test_symlink_escape_blocked(tmp_path):
    cfg = config(tmp_path)
    workspace = Workspace(cfg)
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    link = cfg.root_dir / "link.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(WorkspaceError):
        workspace.read("link.txt")
    with pytest.raises(WorkspaceError):
        workspace.write("link.txt", "changed")
    assert secret.read_text(encoding="utf-8") == "private"


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer integration")
def test_dotenv_in_workspace_does_not_block_sandboxed_bash(tmp_path):
    cfg = config(tmp_path)
    workspace = Workspace(cfg)
    (cfg.root_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=placeholder", encoding="utf-8")
    assert "placeholder" in workspace.read(".env")["content"]
    result = workspace.bash("[IO.File]::WriteAllText('ran.txt','ran')", timeout=30)
    assert result["ok"]
    assert (cfg.root_dir / "ran.txt").read_text(encoding="utf-8") == "ran"


class Client:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete(self, messages, tools, choice):
        self.calls.append([d["function"]["name"] for d in tools])
        return self.response()


def test_cancel_during_model_does_not_start_tools(tmp_path):
    cfg = config(tmp_path)
    cancellation = Event()
    def response():
        cancellation.set()
        return {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
                "function": {"name": "write", "arguments": json.dumps({"path": "not-created.txt", "content": "bad"})}}]}
    client = Client(response)
    agent = Agent(cfg, client)
    try:
        assert agent.run_request("write", cancellation=cancellation) is None
        assert client.calls == [["read", "write", "edit", "bash"]]
        assert not (cfg.root_dir / "not-created.txt").exists()
    finally:
        agent.close()


def test_historical_tool_call_restores_but_cannot_be_reactivated(tmp_path):
    cfg = config(tmp_path)
    client = Client(lambda: {"content": "done"})
    agent = Agent(cfg, client)
    try:
        agent.tool_runtime.restore({"definitions": [{"tool_id": "tool_search", "version": "1",
                                    "schema_fingerprint": "historical"}],
                                    "policy": {"mode": "broad-access"}})
        assert agent.run_request("new task") == "done"
        result = agent.tool_runtime.execute_model_call(agent._runtime_task_id, "tool_search", {})
        assert result["error"]["code"] == "inactive_tool"
        assert client.calls[0] == ["read", "write", "edit", "bash"]
    finally:
        agent.close()


def test_bash_unavailable_never_falls_back_to_host(tmp_path, monkeypatch):
    from tools.shell_sandbox import SandboxUnavailable
    import tools.shell_sandbox as sandbox
    workspace = Workspace(config(tmp_path))
    def unavailable(*args):
        raise SandboxUnavailable("no AppContainer")
    monkeypatch.setattr(sandbox, "start_shell", unavailable)
    with pytest.raises(SandboxUnavailable):
        workspace.bash("write-output denied")


def test_cancellation_before_model_skips_call(tmp_path):
    cfg = config(tmp_path)
    client = Client(lambda: {"content": "unexpected"})
    agent = Agent(cfg, client)
    event = Event()
    event.set()
    try:
        assert agent.run_request("hello", cancellation=event) is None
        assert client.calls == []
    finally:
        agent.close()
