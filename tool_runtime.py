"""Versioned tools, task activation and provider-independent contracts."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from threading import RLock

from jsonschema import Draft202012Validator


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def failure(code, message="", **details):
    return {"ok": False, "error": {"code": code, "message": message, **details}}


@dataclass(frozen=True, init=False)
class ToolMetadata:
    tool_id: str
    version: str
    _schema_json: str
    risk: str
    resources: tuple
    side_effects: tuple
    timeout: float
    output_limit: int
    concurrency: str
    schema_fingerprint: str

    def __init__(self, tool_id, version, schema, risk="low", resources=(), side_effects=(),
                 timeout=30.0, output_limit=32768, concurrency="serial"):
        if not isinstance(tool_id, str) or not tool_id or not isinstance(version, str) or not version:
            raise ValueError("tool_id and version must be non-empty strings")
        if schema.get("name") != tool_id:
            raise ValueError("schema.name must equal tool_id")
        parameters = schema.get("parameters", {"type": "object"})
        Draft202012Validator.check_schema(parameters)

        def local_refs(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in {"$ref", "$dynamicRef"} and not str(value).startswith("#"):
                        raise ValueError("only local schema references are supported")
                    local_refs(value)
            elif isinstance(node, list):
                for value in node:
                    local_refs(value)

        local_refs(parameters)
        if parameters.get("type") != "object":
            raise ValueError("tool parameters must be an object schema")
        if risk not in {"low", "medium", "high"} or concurrency not in {"serial", "parallel"}:
            raise ValueError("invalid risk or concurrency")
        timeout = 30.0 if timeout is None else float(timeout)
        output_limit = 32768 if output_limit is None else int(output_limit)
        if not 0 < timeout <= 3600 or output_limit < 256:
            raise ValueError("invalid runtime limits")
        if any(not isinstance(x, str) or not x for x in (*resources, *side_effects)):
            raise ValueError("resources and side effects must be named strings")
        schema_json = canonical(schema)
        values = dict(tool_id=tool_id, version=version, _schema_json=schema_json,
                      risk=risk, resources=tuple(resources), side_effects=tuple(side_effects),
                      timeout=timeout, output_limit=output_limit, concurrency=concurrency,
                      schema_fingerprint=hashlib.sha256(schema_json.encode()).hexdigest())
        for name, value in values.items():
            object.__setattr__(self, name, value)

    @property
    def schema(self):
        """Return detached JSON, so a caller cannot mutate the registered version."""
        return json.loads(self._schema_json)

    def definition(self):
        return {"tool_id": self.tool_id, "version": self.version, "schema": self.schema,
                "schema_fingerprint": self.schema_fingerprint, "risk": self.risk,
                "resources": list(self.resources), "side_effects": list(self.side_effects),
                "timeout": self.timeout, "output_limit": self.output_limit,
                "concurrency": self.concurrency}


@dataclass(frozen=True)
class RegisteredTool:
    metadata: ToolMetadata
    handler: object
    contextual: bool = False


class ToolRegistry:
    def __init__(self):
        self._tools, self._tasks = {}, {}
        self._lock = RLock()

    def register(self, metadata, handler, *, contextual=False):
        if not callable(handler):
            raise TypeError("handler must be callable")
        key = (metadata.tool_id, metadata.version)
        with self._lock:
            previous = self._tools.get(key)
            if previous:
                if previous.metadata != metadata or previous.handler != handler or previous.contextual != contextual:
                    raise ValueError("inconsistent tool definition; register a new version")
                return previous
            tool = RegisteredTool(metadata, handler, contextual)
            self._tools[key] = tool
            return tool

    def get(self, tool_id, version):
        return self._tools[(tool_id, version)]

    def versions(self, tool_id):
        return tuple(sorted(v for t, v in self._tools if t == tool_id))

    def list(self):
        return tuple(self._tools[key] for key in sorted(self._tools))

    def activate(self, task_id, tool_id, version):
        self.get(tool_id, version)
        keys = self._tasks.setdefault(str(task_id), set())
        if any(t == tool_id and v != version for t, v in keys):
            raise ValueError("another version is already active for this task")
        keys.add((tool_id, version))

    def deactivate(self, task_id, tool_id, version):
        self._tasks.get(str(task_id), set()).discard((tool_id, version))

    def active_keys(self, task_id):
        return frozenset(self._tasks.get(str(task_id), set()))

    def active(self, task_id):
        return tuple(self._tools[key] for key in sorted(self.active_keys(task_id)) if key in self._tools)

    def search(self, query="", *, task_id=None, permission=None, limit=8):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 20:
            raise ValueError("limit must be between 0 and 20")
        names = {t for t, _ in self.active_keys(task_id)} if task_id is not None else set()
        matches = []
        for tool in reversed(self.list()):
            metadata = tool.metadata
            if metadata.tool_id in names or (permission and not permission(metadata)):
                continue
            if str(query).strip().casefold() not in metadata._schema_json.casefold():
                continue
            matches.append(metadata)
            names.add(metadata.tool_id)
        return tuple(sorted(matches, key=lambda m: (m.tool_id, m.version))[:limit])

    def search_tools(self, query="", **kwargs):
        return [dict(m.definition(), active=False) for m in self.search(query, **kwargs)]


class PermissionPolicy:
    """A versioned user grant; a session cannot silently widen it."""
    MODES = {"approve-all": 0, "approve-dangerous": 1, "broad-access": 2}

    def __init__(self, mode="approve-dangerous", *, max_timeout=60.0, max_output=32768,
                 denied_tools=(), on_event=None):
        if mode not in self.MODES or not 0 < max_timeout <= 3600 or max_output < 256:
            raise ValueError("invalid permission policy")
        self._mode, self._revoked, self.version = mode, False, 1
        self.max_timeout, self.max_output = max_timeout, max_output
        self.denied_tools = frozenset(denied_tools)
        self.audit, self.on_event, self._lock = [], on_event, RLock()

    @property
    def mode(self):
        return self._mode

    @property
    def revoked(self):
        return self._revoked

    def _event(self, action, **values):
        event = {"action": action, "policy_version": self.version, **values}
        self.audit.append(event)
        if self.on_event:
            self.on_event(event)

    def change_mode(self, mode, *, confirmed=False):
        if mode not in self.MODES:
            raise ValueError("invalid permission mode")
        with self._lock:
            if self.MODES[mode] > self.MODES[self.mode] and not confirmed:
                self._event("upgrade_denied", requested_mode=mode)
                return failure("confirmation_required", "permission upgrade requires confirmation")
            previous = self.mode
            self._mode = mode
            self.version += 1
            self._event("mode_changed", previous=previous, mode=mode, confirmed=bool(confirmed))
            return {"ok": True}

    def revoke(self):
        with self._lock:
            self._revoked = True
            self.version += 1
            self._event("revoked")

    def visible(self, metadata):
        return metadata.tool_id not in self.denied_tools and not (self.revoked and metadata.side_effects)

    def check(self, metadata, confirmed=False):
        if metadata.tool_id in self.denied_tools:
            return False, "permission_denied"
        if metadata.side_effects and self.revoked:
            return False, "policy_revoked"
        needs_confirmation = self.mode == "approve-all" or self.mode == "approve-dangerous" and metadata.risk == "high"
        if needs_confirmation and not confirmed:
            return False, "confirmation_required"
        return True, None

    def snapshot(self):
        return {"mode": self.mode, "revoked": self.revoked, "version": self.version,
                "max_timeout": self.max_timeout, "max_output": self.max_output,
                "denied_tools": sorted(self.denied_tools)}

    def restore(self, state):
        self._mode = min((self.mode, state.get("mode", self.mode)), key=self.MODES.__getitem__)
        self._revoked = self.revoked or bool(state.get("revoked"))
        self.version = max(self.version, int(state.get("version", 1)))
        self.max_timeout = min(self.max_timeout, state.get("max_timeout", self.max_timeout))
        self.max_output = min(self.max_output, state.get("max_output", self.max_output))
        self.denied_tools |= frozenset(state.get("denied_tools", ()))


class ToolRuntime:
    def __init__(self, registry, stable=(), *, policy=None, confirm=None, recorder=None, resource_resolver=None):
        self.registry, self._stable = registry, tuple(stable)
        self.policy = policy or PermissionPolicy()
        self.confirm, self.recorder, self.history = confirm, recorder, {}
        from tool_execution import ToolDispatcher
        self.dispatcher = ToolDispatcher(registry, policy=self.policy, confirm=confirm,
                                         recorder=recorder, resource_resolver=resource_resolver)

    @property
    def stable_tools(self):
        return tuple(self.registry.get(*key) for key in self._stable)

    def begin_task(self, task_id, compatibility=()):
        self.registry._tasks[str(task_id)] = set()
        for key in (*self._stable, *compatibility):
            self.activate(task_id, *key)

    def activate(self, task_id, tool_id, version):
        metadata = self.registry.get(tool_id, version).metadata
        self.registry.activate(task_id, tool_id, version)
        self.history.setdefault((tool_id, version), metadata.definition())

    def search(self, task_id, query="", *, permission=None, limit=8):
        return self.registry.search(query, task_id=task_id,
                                    permission=permission or self.policy.visible, limit=limit)

    def discover(self, task_id, query="", limit=8):
        matches = self.search(task_id, query, limit=limit)
        bounded = []
        for item in matches:
            trial = [dict(m.definition(), active=True) for m in (*bounded, item)]
            if len(canonical({"ok": True, "tools": trial}).encode()) > self.policy.max_output - 256:
                break
            bounded.append(item)
        for item in bounded:
            self.activate(task_id, item.tool_id, item.version)
        return {"ok": True, "tools": [dict(m.definition(), active=True) for m in bounded],
                "truncated": len(bounded) < len(matches)}

    def schemas(self, task_id, *, native_deferred=False, query="", permission=None, limit=20):
        # Emulated definitions are append-only search results, never prefix tools.
        return [{"type": "function", "function": m.metadata.schema} for m in self.stable_tools]

    def active_definitions(self, task_id):
        return [t.metadata.definition() for t in self.registry.active(task_id)
                if (t.metadata.tool_id, t.metadata.version) not in self._stable]

    def binding(self, task_id, name, version=None, fingerprint=None):
        keys = sorted(key for key in self.registry.active_keys(task_id) if key[0] == name)
        if not keys:
            return None, failure("inactive_tool", "tool is not active for this task", tool_id=name)
        key = keys[0]
        if version is not None and version != key[1]:
            return None, failure("unavailable_version", "requested version is not active")
        try:
            metadata = self.registry.get(*key).metadata
        except KeyError:
            return None, failure("unavailable_version", "registered version is unavailable")
        expected = self.history.get(key, {}).get("schema_fingerprint", metadata.schema_fingerprint)
        if metadata.schema_fingerprint != expected or fingerprint is not None and fingerprint != expected:
            return None, failure("schema_mismatch", "registered schema fingerprint differs")
        return key, None

    def execute(self, task_id, tool_id, version, arguments=None, **kwargs):
        key, error = self.binding(task_id, tool_id, version, kwargs.get("fingerprint"))
        return error or self.dispatcher.execute(*key, arguments, **kwargs)

    def stable_fingerprint(self):
        return hashlib.sha256(canonical(self.schemas("")).encode()).hexdigest()

    fingerprint_stable_prefix = stable_fingerprint

    def snapshot(self):
        return {"definitions": [self.history[key] for key in sorted(self.history)], "policy": self.policy.snapshot()}

    def restore(self, state):
        self.history = {(d["tool_id"], d["version"]): deepcopy(d) for d in state.get("definitions", [])}
        self.policy.restore(state.get("policy", {}))


class ProviderCapabilityMode(str, Enum):
    NATIVE = "native"
    EMULATED = "emulated"


@dataclass
class ProviderSession:
    mode: ProviderCapabilityMode
    native_failures: int = 0
    fallback_count: int = 0

    def __post_init__(self):
        self.mode = ProviderCapabilityMode(self.mode)

    @classmethod
    def from_capability(cls, *, native_deferred):
        return cls("native" if native_deferred else "emulated")

    @property
    def native_deferred(self):
        return self.mode is ProviderCapabilityMode.NATIVE

    def snapshot(self):
        return {"mode": self.mode.value, "native_failures": self.native_failures, "fallback_count": self.fallback_count}


class ProviderLoadError(RuntimeError):
    def __init__(self, message, *, eligible=True):
        super().__init__(message)
        self.eligible = eligible


class ToolProviderAdapter:
    def __init__(self, runtime, session):
        self.runtime, self.session = runtime, session

    @staticmethod
    def _eligible(exc):
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return True
        if getattr(exc, "eligible", None) is not None:
            return bool(exc.eligible)
        value = (str(exc) + " " + str(getattr(exc, "code", ""))).casefold()
        return any(word in value for word in ("network", "timeout", "capability", "unsupported", "deferred", "tool reference"))

    def load(self, task_id, native_loader, *, query="", permission=None, limit=8, emulated_loader=None):
        def emulated():
            return emulated_loader() if emulated_loader else self.runtime.schemas(task_id)
        if not self.session.native_deferred:
            return emulated()
        while True:
            try:
                result = native_loader()
                self.session.native_failures = 0
                return result
            except Exception as exc:
                if not self._eligible(exc):
                    raise
                self.session.native_failures += 1
                if self.session.native_failures < 3:
                    continue
                self.session.mode = ProviderCapabilityMode.EMULATED
                self.session.fallback_count += 1
                return emulated()


NativeProviderAdapter = ToolProviderAdapter

# Preserve public imports while keeping execution separate from registration.
from tool_execution import (ToolCall, ToolDispatcher, ToolScheduler, DependencyScheduler,
                            schedule_tool_calls, execute_tool_calls)
