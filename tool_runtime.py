"""Immutable, versioned tool registry used by the agent runtime."""
from __future__ import annotations
import hashlib, json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Iterable

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
    def __init__(self):
        self._tools: dict[tuple[str,str], RegisteredTool] = {}
        self._tasks: dict[str, set[tuple[str, str]]] = {}
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

    def activate(self, task_id: str, tool_id: str, version: str) -> None:
        """Activate a registered version for one task; registrations are never removed."""
        self.get(tool_id, version)
        self._tasks.setdefault(str(task_id), set()).add((tool_id, version))

    def deactivate(self, task_id: str, tool_id: str, version: str) -> None:
        self._tasks.get(str(task_id), set()).discard((tool_id, version))

    def active(self, task_id: str) -> tuple[RegisteredTool, ...]:
        keys = self._tasks.get(str(task_id), set())
        return tuple(self._tools[key] for key in sorted(keys) if key in self._tools)

    def active_keys(self, task_id: str) -> frozenset[tuple[str, str]]:
        return frozenset(self._tasks.get(str(task_id), set()))

    def search(self, query: str = "", *, task_id: str | None = None,
               permission: Callable[[ToolMetadata], bool] | None = None,
               limit: int = 8) -> tuple[ToolMetadata, ...]:
        """Return deterministic, bounded metadata matches without changing activation."""
        if limit < 0:
            raise ValueError("limit must be non-negative")
        needle = str(query).casefold().strip()
        active = self.active_keys(task_id) if task_id is not None else None
        matches = []
        for key in sorted(self._tools):
            if active is not None and key in active:
                continue
            metadata = self._tools[key].metadata
            haystack = _canonical({"id": metadata.tool_id, "version": metadata.version,
                                   "schema": metadata.schema, "risk": metadata.risk})
            if needle and needle not in haystack.casefold():
                continue
            if permission is not None and not permission(metadata):
                continue
            matches.append(metadata)
        return tuple(matches[:limit])

    def search_tools(self, query: str = "", *, task_id: str | None = None,
                     permission: Callable[[ToolMetadata], bool] | None = None,
                     limit: int = 8) -> list[dict[str, Any]]:
        """Structured discovery response suitable for an emulated provider adapter."""
        return [
            {"tool_id": item.tool_id, "version": item.version,
             "schema": item.schema, "schema_fingerprint": item.schema_fingerprint,
             "risk": item.risk, "active": task_id is not None and
             (item.tool_id, item.version) in self.active_keys(task_id)}
            for item in self.search(query, task_id=task_id, permission=permission, limit=limit)
        ]


class ToolRuntime:
    """Stable-prefix tool serialization with task-scoped emulated discovery."""
    def __init__(self, registry: ToolRegistry, stable: Iterable[tuple[str, str]] = ()):
        self.registry = registry
        self._stable = tuple(stable)

    @property
    def stable_tools(self) -> tuple[RegisteredTool, ...]:
        return tuple(self.registry.get(*key) for key in self._stable)

    def activate(self, task_id: str, tool_id: str, version: str) -> None:
        self.registry.activate(task_id, tool_id, version)

    def search(self, task_id: str, query: str = "", *, permission=None, limit: int = 8):
        return self.registry.search(query, task_id=task_id, permission=permission, limit=limit)

    def schemas(self, task_id: str, *, native_deferred: bool = False,
                query: str = "", permission=None, limit: int = 8) -> list[dict[str, Any]]:
        """Stable definitions always lead; emulated dynamic definitions are appended."""
        result = [{"type": "function", "function": dict(tool.metadata.schema)}
                  for tool in self.stable_tools]
        if native_deferred:
            return result
        dynamic = self.registry.active(task_id)
        if query:
            needle = query.casefold().strip()
            dynamic = tuple(tool for tool in dynamic if needle in _canonical(tool.metadata.schema).casefold())
        if permission is not None:
            dynamic = tuple(tool for tool in dynamic if permission(tool.metadata))
        result.extend({"type": "function", "function": dict(tool.metadata.schema)} for tool in dynamic[:limit])
        return result

    def execute(self, task_id: str, tool_id: str, version: str,
                arguments: Mapping[str, Any] | None = None):
        if (tool_id, version) not in self.registry.active_keys(task_id):
            return {"ok": False, "error": {"code": "inactive_tool", "message": "tool is not active for this task"}}
        return ToolDispatcher(self.registry).execute(tool_id, version, arguments)

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
