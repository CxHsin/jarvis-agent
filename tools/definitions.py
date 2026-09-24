"""Built-in model-facing tool schemas."""
from typing import Any

TOOL_DEFINITIONS: list[dict[str, Any]] = [
 {"type":"function","function":{"name":"read","description":"读取文本文件；支持工作区外的绝对路径，相对路径以工作区为基准。","parameters":{"type":"object","properties":{"path":{"type":"string"},"start_line":{"type":"integer","minimum":1},"end_line":{"type":"integer","minimum":1}},"required":["path"],"additionalProperties":False}}},
 {"type":"function","function":{"name":"edit","description":"编辑工作区文本文件。","parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"},"start_line":{"type":"integer","minimum":1},"end_line":{"type":"integer","minimum":1}},"required":["path","content"],"additionalProperties":False}}},
 {"type":"function","function":{"name":"bash","description":"在 Windows AppContainer 沙箱中通过 PowerShell 执行命令，并非 Bash。不要使用 ls/head 等 Unix 命令。列目录使用 list_directory。读取文件使用 read。","parameters":{"type":"object","properties":{"command":{"type":"string"},"timeout":{"type":"number","minimum":0.1,"maximum":60}},"required":["command"],"additionalProperties":False}}},
 {"type":"function","function":{"name":"tool_search","description":"搜索可用工具。","parameters":{"type":"object","properties":{"query":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":20}},"additionalProperties":False}}},
]
TOOL_DEFINITIONS += [
 {"type":"function","function":{"name":"list_directory","description":"列出工作区目录的直接子项，返回文件和子目录路径及截断标记。查看子目录时再次调用；列目录优先使用本工具，无需 shell。","parameters":{"type":"object","properties":{"path":{"type":"string","description":"工作区内的目录路径，默认当前工作区。"}},"additionalProperties":False}}},
]

TOOL_DEFINITIONS[1]["function"]["parameters"]["properties"]["expected_hash"] = {
    "type": "string", "description": "先前读取的 SHA-256；新文件使用 missing。"}
for _definition in TOOL_DEFINITIONS:
    _properties = _definition["function"]["parameters"]["properties"]
    _properties["_depends_on"] = {"type": "array", "items": {"type": "string"},
                                  "description": "同一批次内必须先成功的 tool_call IDs。"}
    _properties["_version"] = {"type": "string"}
    _properties["_schema_fingerprint"] = {"type": "string"}
