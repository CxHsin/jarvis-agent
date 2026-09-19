"""Moving tool handlers must not narrow protection to the tools directory."""
from types import SimpleNamespace

import pytest

from tools import workspace


@pytest.mark.parametrize("relative", ["agent/agent.py", "memory/memory_service.py", "configuration.py"])
def test_edit_protects_runtime_files_outside_tools(tmp_path, monkeypatch, relative):
    project = tmp_path / "runtime"
    target = project / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("original", encoding="utf-8")
    monkeypatch.setattr(workspace, "__file__", str(project / "tools" / "workspace.py"))
    handler = workspace.Workspace(SimpleNamespace(root_dir=project, state_dir=tmp_path / "state"))

    with pytest.raises(workspace.WorkspaceError, match="运行时代码"):
        handler.edit(relative, "changed")

    assert target.read_text(encoding="utf-8") == "original"
