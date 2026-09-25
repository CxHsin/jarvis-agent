"""The four task-facing tools. Legacy tools remain readable in session history only."""
from typing import Any

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "read", "description": "读取启动工作区内的文本文件。", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}}, "required": ["path"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "write", "description": "在启动工作区内新建或完整重写文本文件。可用 expected_hash 防止覆盖已改变的文件；新文件使用 missing。", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "expected_hash": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "edit", "description": "在启动工作区内局部编辑文本文件；先 read 获取 hash，再传 expected_hash 防止冲突。", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}, "expected_hash": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "bash", "description": "仅在 Windows AppContainer 沙箱中执行 PowerShell 命令；沙箱不可用则拒绝，不回退到宿主执行。", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "number", "minimum": 0.1, "maximum": 60}}, "required": ["command"], "additionalProperties": False}}},
]
for _definition in TOOL_DEFINITIONS:
    _properties = _definition["function"]["parameters"]["properties"]
    _properties["_depends_on"] = {"type": "array", "items": {"type": "string"}, "description": "同一批次内必须先成功的 tool_call IDs。"}
    _properties["_version"] = {"type": "string"}
    _properties["_schema_fingerprint"] = {"type": "string"}
