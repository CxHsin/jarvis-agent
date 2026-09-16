"""Validated execution, cooperative cancellation and dependency scheduling."""
from __future__ import annotations

import inspect
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Event, RLock, Thread

from jsonschema import Draft202012Validator


def failure(code, message="", **details):
    return {"ok": False, "error": {"code": code, "message": message, **details}}


class ExecutionStopped(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ExecutionContext:
    """Built-in handlers check cancellation before committing side effects."""
    def __init__(self, timeout, cancellation, policy, metadata, policy_version):
        self.deadline = time.monotonic() + timeout
        self.cancellation = cancellation
        self.stopped = Event()
        self.policy, self.metadata, self.policy_version = policy, metadata, policy_version
        self._commit_lock = RLock()

    def check(self):
        if self.cancellation.is_set():
            raise ExecutionStopped("cancelled")
        if self.stopped.is_set() or time.monotonic() >= self.deadline:
            raise ExecutionStopped("timeout")
        if self.metadata.side_effects:
            if self.policy.revoked:
                raise ExecutionStopped("policy_revoked")
            if self.policy.version != self.policy_version:
                raise ExecutionStopped("policy_changed")

    def commit(self, action):
        with self._commit_lock, self.policy._lock:
            self.check()
            return action()

    def stop(self):
        with self._commit_lock:
            self.stopped.set()


class ToolDispatcher:
    def __init__(self, registry, *, policy=None, confirm=None, recorder=None, resource_resolver=None):
        from tool_runtime import PermissionPolicy
        self.registry, self.policy = registry, policy or PermissionPolicy()
        self.confirm, self.recorder = confirm, recorder
        self.resource_resolver = resource_resolver
        self.audit = []
        # Dispatchers over one registry share leases, including timed-out workers.
        with registry._lock:
            if not hasattr(registry, "_execution_leases"):
                registry._execution_leases = {}
        self._busy = registry._execution_leases
        self._confirmation_lock = RLock()

    def resources(self, metadata, arguments):
        resources = set(metadata.resources)
        if self.resource_resolver:
            resources.update(self.resource_resolver(metadata, arguments))
        if metadata.concurrency == "serial":
            resources.add("*")
        return resources

    @staticmethod
    def overlaps(left, right):
        return bool("*" in left or "*" in right or left & right)

    def record(self, event):
        with self.registry._lock:
            self.audit.append(deepcopy(event))
            if self.recorder:
                self.recorder(event)

    def authorize(self, metadata, arguments, cancellation, call_id=""):
        """Run confirmations on the scheduling thread, before workers start."""
        errors = list(Draft202012Validator(metadata.schema.get("parameters", {"type": "object"})).iter_errors(arguments))
        if errors:
            return None, failure("invalid_arguments", errors[0].message)
        with self._confirmation_lock:
            version = self.policy.version
            allowed, reason = self.policy.check(metadata)
            if not allowed and reason == "confirmation_required" and self.confirm:
                try:
                    confirmed = bool(self.confirm(metadata, deepcopy(arguments)))
                except KeyboardInterrupt:
                    cancellation.set()
                    return None, dict(failure("cancelled"), cancelled=True)
                except Exception as exc:
                    return None, failure("confirmation_failed", str(exc))
                self.record({"phase": "confirmation", "tool_id": metadata.tool_id,
                             "call_id": call_id, "policy_version": version, "confirmed": confirmed})
                allowed, reason = self.policy.check(metadata, confirmed=confirmed)
            if cancellation.is_set():
                return None, dict(failure("cancelled"), cancelled=True)
            if version != self.policy.version:
                return None, failure("policy_changed", "policy changed during confirmation")
            return (version, None) if allowed else (None, failure(reason, "tool execution is not authorized"))

    def execute(self, tool_id, version, arguments=None, *, fingerprint=None, timeout=None,
                cancellation=None, call_id="", authorization=None):
        event = {"tool_id": tool_id, "version": version, "call_id": call_id,
                 "policy_version": self.policy.version, "time": time.time()}
        output_limit = self.policy.max_output

        def finish(result):
            try:
                payload = json.dumps(result, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                result = failure("invalid_result", str(exc))
                payload = json.dumps(result, ensure_ascii=False)
            if len(payload.encode("utf-8")) > output_limit:
                if result.get("ok"):
                    bounded = {"ok": True, "result": {"ok": True, "truncated": True, "preview": ""}}
                    preview_target = bounded["result"]
                else:
                    bounded = failure(str((result.get("error") or {}).get("code", "tool_failed"))[:64])
                    bounded["truncated"] = True
                    preview_target = bounded["error"]
                low, high = 0, len(payload)
                while low < high:
                    middle = (low + high + 1) // 2
                    preview_target["preview"] = payload[:middle]
                    if len(json.dumps(bounded, ensure_ascii=False).encode("utf-8")) <= output_limit:
                        low = middle
                    else:
                        high = middle - 1
                preview_target["preview"] = payload[:low]
                result = bounded
            completed = dict(event, phase="finished", ok=result.get("ok", False),
                             error=result.get("error"), uncertain=result.get("uncertain", False))
            self.record(completed)
            return dict(result, audit=completed)

        try:
            tool = self.registry.get(tool_id, version)
        except KeyError:
            return finish(failure("unavailable_version", "tool version is not registered"))
        metadata = tool.metadata
        output_limit = min(metadata.output_limit, output_limit)
        event["schema_fingerprint"] = metadata.schema_fingerprint
        if fingerprint is not None and fingerprint != metadata.schema_fingerprint:
            return finish(failure("schema_mismatch", "schema fingerprint differs"))
        args = deepcopy({} if arguments is None else arguments)
        try:
            errors = list(Draft202012Validator(metadata.schema.get("parameters", {"type": "object"})).iter_errors(args))
            if errors:
                return finish(failure("invalid_arguments", errors[0].message))
            json.dumps(args, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            return finish(failure("invalid_arguments", str(exc)))
        cancellation = cancellation or Event()
        if cancellation.is_set():
            return finish(dict(failure("cancelled"), cancelled=True))
        policy_version = authorization
        if policy_version is None:
            policy_version, error = self.authorize(metadata, args, cancellation, call_id)
            if error:
                return finish(error)
        if policy_version != self.policy.version:
            return finish(failure("policy_changed", "policy changed before execution"))
        allowed, reason = self.policy.check(metadata, confirmed=True)
        if not allowed:
            return finish(failure(reason))
        try:
            resources = self.resources(metadata, args)
            duration = min(metadata.timeout, self.policy.max_timeout,
                           float(timeout) if timeout is not None else metadata.timeout)
            if duration <= 0:
                raise ValueError("timeout must be positive")
        except (ValueError, TypeError, OSError) as exc:
            return finish(failure("invalid_arguments", str(exc)))
        with self.registry._lock:
            if any(self.overlaps(resources, held) for held in self._busy.values()):
                return finish(failure("resource_conflict", "resource is still in use"))
            lease = object()
            self._busy[lease] = resources
        context = ExecutionContext(duration, cancellation, self.policy, metadata, policy_version)
        done, result_box = Event(), []
        self.record(dict(event, phase="started"))

        def invoke():
            try:
                context.check()
                result = tool.handler(context, **args) if tool.contextual else tool.handler(**args)
                if inspect.isawaitable(result):
                    if hasattr(result, "close"):
                        result.close()
                    raise TypeError("synchronous tool handler required")
                context.check()
                if isinstance(result, dict) and result.get("ok") is False:
                    if not isinstance(result.get("error"), dict):
                        result = dict(result, error={"code": "tool_failed", "message": str(result.get("error", "tool reported failure"))})
                    result_box.append(result)
                else:
                    result_box.append({"ok": True, "result": result})
            except ExecutionStopped as exc:
                result_box.append(dict(failure(exc.code), cancelled=exc.code == "cancelled",
                                       uncertain=bool(metadata.side_effects)))
            except (KeyboardInterrupt, SystemExit):
                cancellation.set()
                result_box.append(dict(failure("cancelled"), cancelled=True))
            except TimeoutError as exc:
                result_box.append(failure("timeout", str(exc)))
            except Exception as exc:
                result_box.append(failure("execution_error", str(exc)))
            finally:
                with self.registry._lock:
                    self._busy.pop(lease, None)
                done.set()

        worker = Thread(target=invoke, daemon=True, name=f"tool-{tool_id}")
        worker.start()
        try:
            while not done.wait(0.01):
                if cancellation.is_set() or time.monotonic() >= context.deadline:
                    context.stop()
                    # Uncooperative extensions can still run; keep their resource lease.
                    code = "cancelled" if cancellation.is_set() else "timeout"
                    return finish(dict(failure(code), cancelled=code == "cancelled",
                                       uncertain=bool(metadata.side_effects)))
        except KeyboardInterrupt:
            cancellation.set()
            context.stop()
            return finish(dict(failure("cancelled"), cancelled=True, uncertain=bool(metadata.side_effects)))
        return finish(result_box[0])


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    tool_id: str
    version: str
    arguments: dict = field(default_factory=dict)
    depends_on: tuple = ()
    fingerprint: str | None = None

    def __post_init__(self):
        if not self.call_id:
            raise ValueError("call_id is required")
        object.__setattr__(self, "depends_on", tuple(self.depends_on))


def references(value):
    if isinstance(value, dict):
        if set(value) == {"$result"}:
            ref = value["$result"]
            if not isinstance(ref, dict) or not isinstance(ref.get("call_id"), str):
                raise ValueError("$result requires a call_id and optional path array")
            yield ref["call_id"]
        else:
            for item in value.values():
                yield from references(item)
    elif isinstance(value, list):
        for item in value:
            yield from references(item)


def resolve_results(value, results):
    if isinstance(value, dict):
        if set(value) == {"$result"}:
            ref = value["$result"]
            result = results[ref["call_id"]].get("result")
            for part in ref.get("path", []):
                result = result[part]
            return deepcopy(result)
        return {key: resolve_results(item, results) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_results(item, results) for item in value]
    return value


class ToolScheduler:
    def __init__(self, registry, *, max_workers=8, dispatcher=None):
        self.registry, self.max_workers = registry, max(1, int(max_workers))
        self.dispatcher = dispatcher or ToolDispatcher(registry)

    def schedule(self, calls):
        calls = tuple(calls)
        by_id = {call.call_id: call for call in calls}
        if len(by_id) != len(calls):
            return failure("duplicate_call_id")
        dependencies, resources = {}, {}
        for call in calls:
            try:
                metadata = self.registry.get(call.tool_id, call.version).metadata
                dependencies[call.call_id] = set(call.depends_on) | set(references(call.arguments))
                # Resource references are not yet resolved: conservatively isolate them.
                resources[call.call_id] = ({"*"} if list(references(call.arguments)) else
                                           self.dispatcher.resources(metadata, call.arguments))
            except KeyError:
                return failure("unavailable_version", tool_id=call.tool_id, version=call.version)
            except (ValueError, TypeError, OSError) as exc:
                return failure("invalid_arguments", str(exc))
            if dependencies[call.call_id] - by_id.keys():
                return failure("missing_dependency", call_id=call.call_id)
        remaining, waves = list(by_id), []
        while remaining:
            ready = [cid for cid in remaining if not dependencies[cid].intersection(remaining)]
            if not ready:
                return failure("dependency_cycle", calls=remaining)
            wave, used = [], set()
            for cid in ready:
                current = resources[cid]
                if wave and ("*" in current or "*" in used or current & used):
                    continue
                wave.append(cid)
                used.update(current)
            waves.append(tuple(wave))
            remaining = [cid for cid in remaining if cid not in wave]
        return {"ok": True, "waves": tuple(waves), "dependencies": dependencies}

    def execute(self, calls, *, cancellation=None, execute=None, on_result=None):
        calls = tuple(calls)
        plan = self.schedule(calls)
        if not plan["ok"]:
            return plan
        by_id, results = {call.call_id: call for call in calls}, {}
        cancellation = cancellation or Event()

        def run(cid):
            call = by_id[cid]
            if cancellation.is_set():
                return dict(failure("cancelled"), cancelled=True)
            if any(not results[dep].get("ok") for dep in plan["dependencies"][cid]):
                return failure("dependency_failed", call_id=cid)
            args, grant, error = prepared[cid]
            if error:
                return error
            if execute:
                return execute(call, args, cancellation, grant)
            return self.dispatcher.execute(call.tool_id, call.version, args,
                                           fingerprint=call.fingerprint, call_id=cid, cancellation=cancellation,
                                           authorization=grant)

        for wave in plan["waves"]:
            prepared = {}
            for cid in wave:
                call = by_id[cid]
                if any(not results[dep].get("ok") for dep in plan["dependencies"][cid]):
                    prepared[cid] = ({}, None, failure("dependency_failed"))
                    continue
                try:
                    args = resolve_results(call.arguments, results)
                    grant, error = self.dispatcher.authorize(self.registry.get(call.tool_id, call.version).metadata,
                                                             args, cancellation, cid)
                    prepared[cid] = (args, grant, error)
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    prepared[cid] = ({}, None, failure("invalid_result_reference", str(exc)))
            pool = ThreadPoolExecutor(max_workers=min(self.max_workers, len(wave)))
            futures = {pool.submit(run, cid): cid for cid in wave}
            try:
                for future in as_completed(futures):
                    cid = futures[future]
                    try:
                        results[cid] = future.result()
                    except Exception as exc:
                        results[cid] = failure("recovery_error", str(exc))
            except KeyboardInterrupt:
                cancellation.set()
                for future, cid in futures.items():
                    results[cid] = future.result()
            finally:
                pool.shutdown(wait=True)
            # Persist and display results on the caller thread in request order.
            for cid in wave:
                if on_result:
                    on_result(by_id[cid], results[cid])
        return {"ok": True, "results": results, "waves": plan["waves"]}


DependencyScheduler = ToolScheduler


def schedule_tool_calls(registry, calls, *, max_workers=8):
    return ToolScheduler(registry, max_workers=max_workers).schedule(calls)


def execute_tool_calls(registry, calls, *, max_workers=8):
    return ToolScheduler(registry, max_workers=max_workers).execute(calls)
