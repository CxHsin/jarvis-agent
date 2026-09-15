"""Immutable, versioned tool registry used by the agent runtime."""
from __future__ import annotations
import hashlib, json
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Iterable
from enum import Enum

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
        stable_keys = set(self._stable)
        dynamic = tuple(tool for tool in self.registry.active(task_id)
                        if (tool.metadata.tool_id, tool.metadata.version) not in stable_keys)
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

    def stable_fingerprint(self) -> str:
        """Fingerprint only the stable prefix; dynamic task history cannot affect it."""
        payload = [_canonical(tool.metadata.schema) for tool in self.stable_tools]
        return hashlib.sha256(_canonical(payload).encode()).hexdigest()

    fingerprint_stable_prefix = stable_fingerprint


class ProviderCapabilityMode(str, Enum):
    """The provider loading contract selected for a session."""
    NATIVE = "native"
    EMULATED = "emulated"


@dataclass
class ProviderSession:
    """Persistent provider state; reuse this object for every model call."""
    mode: ProviderCapabilityMode
    native_failures: int = 0
    fallback_count: int = 0

    def __post_init__(self):
        self.mode = ProviderCapabilityMode(self.mode)

    @classmethod
    def from_capability(cls, *, native_deferred: bool) -> "ProviderSession":
        """Create explicit session state from provider capability discovery."""
        return cls(ProviderCapabilityMode.NATIVE if native_deferred
                    else ProviderCapabilityMode.EMULATED)

    @property
    def native_deferred(self) -> bool:
        return self.mode is ProviderCapabilityMode.NATIVE


class ProviderLoadError(RuntimeError):
    """An error carrying whether retrying native loading is meaningful."""
    def __init__(self, message: str, *, eligible: bool = True):
        super().__init__(message)
        self.eligible = eligible


class ToolProviderAdapter:
    """Load native references when supported, with one safe emulated fallback.

    ``native_loader`` may return a list of references or raise an exception.
    The result is committed only after the call succeeds, so partial native
    references can never leak into the emulated retry.
    """
    def __init__(self, runtime: ToolRuntime, session: ProviderSession):
        self.runtime, self.session = runtime, session

    @staticmethod
    def _eligible(exc: BaseException) -> bool:
        if isinstance(exc, ProviderLoadError):
            return exc.eligible
        value = str(exc).casefold()
        if getattr(exc, "eligible", None) is not None:
            return bool(exc.eligible)
        code = str(getattr(exc, "code", "")).casefold()
        return any(word in value or word in code for word in
                   ("network", "timeout", "capability", "unsupported", "deferred", "tool reference"))

    def load(self, task_id: str, native_loader: Callable[[], Any], *,
             query: str = "", permission=None, limit: int = 8,
             emulated_loader: Callable[[], Any] | None = None) -> Any:
        """Return provider references and persist the selected loading mode."""
        if self.session.mode is ProviderCapabilityMode.EMULATED:
            return self._emulated(task_id, query, permission, limit, emulated_loader)
        while True:
            try:
                # Do not expose a mutable/partial native response until success.
                result = native_loader()
                self.session.native_failures = 0
                return result
            except BaseException as exc:
                if not self._eligible(exc):
                    raise
                self.session.native_failures += 1
                if self.session.native_failures < 3:
                    continue
                self.session.mode = ProviderCapabilityMode.EMULATED
                self.session.fallback_count += 1
                return self._emulated(task_id, query, permission, limit, emulated_loader)

    def _emulated(self, task_id: str, query: str, permission, limit: int,
                  loader: Callable[[], Any] | None) -> Any:
        if loader is not None:
            return loader()
        return self.runtime.schemas(task_id, native_deferred=False,
                                    query=query, permission=permission, limit=limit)


# Descriptive alias for callers that model the native provider as an adapter.
NativeProviderAdapter = ToolProviderAdapter

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


@dataclass(frozen=True)
class ToolCall:
    """A scheduled invocation and its explicit result dependencies."""
    call_id: str
    tool_id: str
    version: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.call_id:
            raise ValueError("call_id is required")
        object.__setattr__(self, "depends_on", tuple(self.depends_on))


class ToolScheduler:
    """Dependency and resource aware execution for a batch of tool calls."""
    def __init__(self, registry: ToolRegistry, *, max_workers: int = 8):
        self.registry = registry
        self.max_workers = max(1, int(max_workers))
        self.dispatcher = ToolDispatcher(registry)

    def _validate(self, calls):
        calls = tuple(calls)
        by_id = {}
        for call in calls:
            if call.call_id in by_id:
                return None, {"ok": False, "error": {"code": "duplicate_call_id", "call_id": call.call_id}}
            by_id[call.call_id] = call
            try:
                self.registry.get(call.tool_id, call.version)
            except KeyError:
                return None, {"ok": False, "error": {"code": "unavailable_version", "call_id": call.call_id,
                                                         "tool_id": call.tool_id, "version": call.version}}
        for call in calls:
            missing = [dep for dep in call.depends_on if dep not in by_id]
            if missing:
                return None, {"ok": False, "error": {"code": "missing_dependency", "call_id": call.call_id,
                                                         "dependencies": missing}}
        return by_id, None

    def schedule(self, calls):
        """Return deterministic execution waves, or a structured validation outcome."""
        by_id, error = self._validate(calls)
        if error:
            return error
        remaining = set(by_id)
        waves = []
        while remaining:
            ready = [cid for cid in sorted(remaining)
                     if all(dep not in remaining for dep in by_id[cid].depends_on)]
            if not ready:
                return {"ok": False, "error": {"code": "dependency_cycle",
                                                 "calls": sorted(remaining)}}
            wave = []
            used = set()
            for cid in ready:
                resources = set(self.registry.get(by_id[cid].tool_id, by_id[cid].version).metadata.resources)
                if resources & used:
                    continue
                wave.append(cid); used.update(resources)
            if not wave:
                return {"ok": False, "error": {"code": "resource_conflict", "calls": ready}}
            waves.append(tuple(wave)); remaining.difference_update(wave)
        return {"ok": True, "waves": tuple(waves)}

    def execute(self, calls):
        """Execute independent calls concurrently, preserving dependency waves."""
        plan = self.schedule(calls)
        if not plan.get("ok"):
            return plan
        by_id = {call.call_id: call for call in calls}
        results = {}
        for wave in plan["waves"]:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(wave))) as pool:
                futures = {pool.submit(self.dispatcher.execute, by_id[cid].tool_id,
                                        by_id[cid].version, by_id[cid].arguments): cid for cid in wave}
                wait(futures)
                for future, cid in futures.items():
                    try:
                        results[cid] = future.result()
                    except Exception as exc:
                        results[cid] = {"ok": False, "error": {"code": "recovery_error", "message": str(exc)}}
        return {"ok": True, "results": {cid: results[cid] for cid in sorted(results)}, "waves": plan["waves"]}


# Short alias for integrations that call this a dependency scheduler.
DependencyScheduler = ToolScheduler

def schedule_tool_calls(registry: ToolRegistry, calls, *, max_workers: int = 8):
    return ToolScheduler(registry, max_workers=max_workers).schedule(calls)

def execute_tool_calls(registry: ToolRegistry, calls, *, max_workers: int = 8):
    return ToolScheduler(registry, max_workers=max_workers).execute(calls)

class PermissionPolicy:
    MODES = {"approve-all", "approve-dangerous", "broad-access"}
    def __init__(self, mode="approve-dangerous"): self.mode = mode; self.revoked = False
    def check(self, metadata: ToolMetadata, confirmed=False):
        if self.revoked: return False, "policy_revoked"
        if self.mode == "broad-access" or (self.mode == "approve-all" and not metadata.side_effects): return True, None
        if metadata.side_effects and not confirmed: return False, "confirmation_required"
        return True, None
