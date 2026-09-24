import os
from types import SimpleNamespace

import pytest

from agent.agent import Agent
from tools.definitions import TOOL_DEFINITIONS
from tools.workspace import decode_shell_output


@pytest.mark.skipif(os.name != "nt", reason="Windows OEM code page")
def test_native_cmd_error_is_readable():
    error = "拒绝访问。\r\n"
    assert decode_shell_output(error.encode("oem")) == error


def test_utf8_program_output_is_preserved():
    output = "文件清单：笔记.md"
    assert decode_shell_output(output.encode("utf-8")) == output


def test_failed_shell_preview_includes_cause_and_exit_code():
    agent = SimpleNamespace(config=SimpleNamespace(
        verbose_tool_output=False, tool_output_preview_chars=500))
    preview = Agent._tool_result_for_display(agent, "bash", {
        "ok": False, "exit_code": 1, "output": "拒绝访问。\r\n",
        "error": {"code": "tool_failed", "message": "tool reported failure"},
    })
    assert "拒绝访问。" in preview
    assert "exit_code=1" in preview
    assert "tool=bash" in preview


def test_shell_schema_explains_interpreter_and_fail_closed_boundary():
    schema = next(d["function"] for d in TOOL_DEFINITIONS if d["function"]["name"] == "bash")
    assert "PowerShell" in schema["description"]
    assert "AppContainer" in schema["description"]
    assert "list_directory" not in schema["description"]
