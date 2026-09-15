"""Immutable, versioned tool registry used by the agent runtime."""
from __future__ import annotations
import hashlib, json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

@dataclass(frozen=True)
class ToolMetadata:
    tool_id: str
    version: str
    schema: Mapping[str, Any]
    risk: str = "low"
    resources: tuple[str, ...] = ()
    side_effects: tuple[str, ...] = ()
    timeout: float | None = None
    output_limit: int | None = None
    concurrency: str = "serial"
    schema_fingerprint: str = field(init=False)
    def __post_init__(self):
        if not self.tool_id or not self.version: raise ValueError("tool_id and version are required")
        object.__setattr__(self, "schema", json.loads(_canonical(self.schema)))
        fp = hashlib.sha256(_canonical(self.schema).encode()).hexdigest()
        object.__setattr__(self, "schema_fingerprint", fp)

@dataclass(frozen=True)
class RegisteredTool:
    metadata: ToolMetadata
    handler: Callable[..., Any]

class ToolRegistry:
    def __init__(self): self._tools: dict[tuple[str,str], RegisteredTool] = {}
    def register(self, metadata: ToolMetadata, handler: Callable[..., Any]) -> RegisteredTool:
        key=(metadata.tool_id, metadata.version)
        if key in self._tools:
            old=self._tools[key].metadata
            if old.schema_fingerprint != metadata.schema_fingerprint or old != metadata: raise ValueError("inconsistent tool definition")
            return self._tools[key]
        if not callable(handler): raise TypeError("handler must be callable")
        self._tools[key]=RegisteredTool(metadata,handler); return self._tools[key]
    def get(self, tool_id: str, version: str) -> RegisteredTool: return self._tools[(tool_id,version)]
    def versions(self, tool_id: str) -> tuple[str,...]: return tuple(sorted(v for (t,v) in self._tools if t==tool_id))
    def list(self) -> tuple[RegisteredTool,...]: return tuple(self._tools[k] for k in sorted(self._tools))

class ToolDispatcher:
    """Validated, audited execution seam for registered tools."""
    def __init__(self, registry: ToolRegistry):
        self.registry, self.audit = registry, []
        self._busy: set[str] = set()
    def execute(self, tool_id: str, version: str, arguments: Mapping[str, Any] | None = None, *, timeout: float | None = None):
        import inspect
        tool = self.registry.get(tool_id, version); args = dict(arguments or {})
        event = {"tool_id": tool_id, "version": version, "schema_fingerprint": tool.metadata.schema_fingerprint}
        resource = tuple(tool.metadata.resources)
        if any(r in self._busy for r in resource):
            return {"ok": False, "error": {"code": "resource_conflict", "message": "resource is busy"}, "audit": event}
        self._busy.update(resource)
        try:
            result = tool.handler(**args)
            if inspect.isawaitable(result): raise TypeError("async handlers are not supported by synchronous dispatcher")
            event.update({"ok": True}); self.audit.append(event)
            return {"ok": True, "result": result, "audit": event}
        except TimeoutError as exc:
            event.update({"ok": False, "error": "timeout"}); self.audit.append(event)
            return {"ok": False, "error": {"code": "timeout", "message": str(exc)}, "audit": event}
        except Exception as exc:
            event.update({"ok": False, "error": type(exc).__name__}); self.audit.append(event)
            return {"ok": False, "error": {"code": "execution_error", "message": str(exc)}, "audit": event}
        finally: self._busy.difference_update(resource)

class PermissionPolicy:
    MODES = {"approve-all", "approve-dangerous", "broad-access"}
    def __init__(self, mode="approve-dangerous"): self.mode = mode; self.revoked = False
    def check(self, metadata: ToolMetadata, confirmed=False):
        if self.revoked: return False, "policy_revoked"
        if self.mode == "broad-access" or (self.mode == "approve-all" and not metadata.side_effects): return True, None
        if metadata.side_effects and not confirmed: return False, "confirmation_required"
        return True, None
