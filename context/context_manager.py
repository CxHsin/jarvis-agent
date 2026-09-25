"""Runtime context bookkeeping for the Jarvis agent.

The model loop stays in :mod:`jarvis_agent`; this module owns the dynamic
information that is appended to or projected into each request.  Compaction
itself lives in :mod:`compaction`; here we only decide when to run it and what
the outcome means for the current context.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Mapping, Sequence

from models.cache_metrics import cache_usage
from context.compaction import (
    CONTEXT_COMPRESSED_MARKER,
    CONTEXT_FALLBACK_MARKER,
    CONTEXT_SUMMARY_CLOSE,
    CONTEXT_SUMMARY_OPEN,
    CompactionResult,
    CompactionService,
)
from context.context_budget import ContextBudget, estimate_tokens


CONTEXT_RECOVERED_MARKER = "[CONTEXT_RECOVERED]"


class ContextManager:
    """Maintain session evidence, task state, budget and compaction requests."""

    _SNAPSHOT_FIELDS = (
        "task_number",
        "current_task",
        "session_evidence",
        "session_archive",
        "_evidence_by_key",
        "_seen_call_keys",
        "last_usage_tokens",
        "last_usage_method",
        "_usage_anchor",
        "last_metrics",
        "last_compression_event",
        "compaction_failures",
    )

    def __init__(self, config: Any, recorder: Any = None):
        self.config = config
        self.recorder = recorder
        self.budget = ContextBudget.from_config(config)
        self.task_number = 0
        self.current_task: dict[str, Any] = {}
        self.session_evidence: list[dict[str, Any]] = []
        self.session_archive: list[dict[str, Any]] = []
        self._evidence_by_key: dict[str, str] = {}
        self._seen_call_keys: set[str] = set()
        self.last_usage_tokens: int | None = None
        self.last_usage_method: str | None = None
        self._usage_anchor = None
        self.last_metrics: dict[str, Any] = {}
        self.last_compression_event: CompactionResult | None = None
        self.compaction_failures = 0
        self.compactor = CompactionService(config, recorder)

    def select_messages(self, store: Any, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Project the committed event stream into the next task's visible history."""
        selected = store.context_messages()
        if selected != messages[1:]:
            store.record_recent_context(selected)
        return selected

    def begin_task(self, goal: str) -> None:
        self.task_number += 1
        self.current_task = {
            "number": self.task_number,
            "goal": goal,
            "tool_calls": 0,
            "repeated_calls": 0,
            "errors": [],
            "events": [],
            "round": 0,
        }
        if self.recorder is not None:
            self.recorder.record_task(self.task_number, goal)

    def snapshot(self) -> dict[str, Any]:
        """Capture every mutable field so a failed request can be undone."""

        state = {name: deepcopy(getattr(self, name)) for name in self._SNAPSHOT_FIELDS}
        # The circuit breaker lives in the service; keep it in the same snapshot.
        state["compaction_failures"] = self.compactor.failures
        return state

    def restore(self, state: Mapping[str, Any]) -> None:
        """Undo a failed request back to a snapshot taken by :meth:`snapshot`."""

        for name in self._SNAPSHOT_FIELDS:
            if name == "compaction_failures":
                continue
            setattr(self, name, deepcopy(state[name]))
        self.compaction_failures = state.get("compaction_failures", 0)
        self.compactor.failures = self.compaction_failures

    def restore_session(
        self,
        entries: Sequence[Mapping[str, Any]],
        compressed_call_ids: Sequence[str] = (),
    ) -> None:
        """Rebuild evidence and archive state from persisted archive entries."""

        self.session_archive = []
        self.session_evidence = []
        self._evidence_by_key = {}
        self._seen_call_keys = set()
        compressed = {str(call_id) for call_id in compressed_call_ids}
        for entry in entries:
            name = str(entry.get("tool", ""))
            arguments = entry.get("arguments") if isinstance(entry.get("arguments"), Mapping) else {}
            result = entry.get("result") if isinstance(entry.get("result"), Mapping) else {}
            call_id = str(entry.get("call_id", ""))
            try:
                task_number = int(entry.get("task") or 0)
            except (TypeError, ValueError):
                task_number = 0
            key = self._canonical_key(name, arguments)
            self._seen_call_keys.add(key)
            refs = self._source_refs(name, result)
            evidence_id: str | None = None
            if result.get("ok") and refs:
                evidence_id = self._evidence_by_key.get(key)
                if evidence_id is None:
                    evidence_id = f"E{len(self.session_evidence) + 1}"
                    self._evidence_by_key[key] = evidence_id
                    self.session_evidence.append(
                        {
                            "id": evidence_id,
                            "task": task_number,
                            "tool": name,
                            "refs": refs,
                            "repeated": False,
                            "compressed": False,
                        }
                    )
                else:
                    for evidence in self.session_evidence:
                        if evidence["id"] == evidence_id:
                            evidence["repeated"] = True
            self.session_archive.append(
                {
                    "task": task_number,
                    "call_id": call_id,
                    "tool": name,
                    "arguments": dict(arguments),
                    "result": dict(result),
                    "evidence_id": evidence_id,
                    "compressed": call_id in compressed,
                }
            )
        compressed_evidence = {
            entry["evidence_id"]
            for entry in self.session_archive
            if entry.get("compressed") and entry.get("evidence_id")
        }
        for evidence in self.session_evidence:
            evidence["compressed"] = evidence["id"] in compressed_evidence

    def set_round(self, round_number: int) -> None:
        if self.current_task:
            self.current_task["round"] = round_number

    @staticmethod
    def _canonical_key(name: str, arguments: Mapping[str, Any]) -> str:
        return json.dumps(
            {"name": name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    @staticmethod
    def _source_refs(name: str, result: Mapping[str, Any]) -> list[str]:
        if not result.get("ok"):
            return []
        if name in {"read", "read_file"} and result.get("path"):
            start = result.get("start_line", "?")
            end = result.get("end_line", start)
            return [f"{result['path']}:{start}-{end}"]
        if name == "search_file_content":
            refs = [f"{item.get('path', '?')}:{item.get('line', '?')}" for item in result.get("matches", [])]
            return refs or [f"search:{result.get('query', '')} (no matches)"]
        if name == "list_directory" and result.get("path"):
            return [f"directory:{result['path']}"]
        return []

    def record_tool_result(
        self,
        name: str,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
        call_id: str,
    ) -> None:
        if not self.current_task:
            self.begin_task("未命名任务")
        key = self._canonical_key(name, arguments)
        repeated = key in self._seen_call_keys
        self._seen_call_keys.add(key)
        self.current_task["tool_calls"] += 1
        if repeated:
            self.current_task["repeated_calls"] += 1

        evidence_id: str | None = None
        refs = self._source_refs(name, result)
        if result.get("ok") and refs:
            evidence_id = self._evidence_by_key.get(key)
            if evidence_id is None:
                evidence_id = f"E{len(self.session_evidence) + 1}"
                self._evidence_by_key[key] = evidence_id
                self.session_evidence.append(
                    {
                        "id": evidence_id,
                        "task": self.task_number,
                        "tool": name,
                        "refs": refs,
                        "repeated": False,
                        "compressed": False,
                    }
                )
            elif evidence_id in {item["id"] for item in self.session_evidence}:
                repeated = True
                for evidence in self.session_evidence:
                    if evidence["id"] == evidence_id:
                        evidence["repeated"] = True

        if not result.get("ok"):
            error = str(result.get("error", "工具调用失败"))
            self.current_task["errors"].append(error)

        event = {
            "call_id": call_id,
            "tool": name,
            "arguments": dict(arguments),
            "repeated": repeated,
            "evidence_id": evidence_id,
            "refs": refs,
            "ok": bool(result.get("ok")),
        }
        self.current_task["events"].append(event)
        archive_entry = {
            "task": self.task_number,
            "call_id": call_id,
            "tool": name,
            "arguments": dict(arguments),
            "result": dict(result),
            "evidence_id": evidence_id,
            "compressed": False,
        }
        self.session_archive.append(archive_entry)
        if self.recorder is not None:
            self.recorder.record_archive(archive_entry)

    def record_usage(self, usage: Mapping[str, Any] | None, messages=None, tools=None) -> None:
        self.last_usage_tokens = cache_usage(usage)["input"]
        self.last_usage_method = "actual" if self.last_usage_tokens is not None else None
        self._usage_anchor = None
        if self.last_usage_tokens is not None and messages:
            # The previous request is the stable prefix for the next estimate.
            self._usage_anchor = (deepcopy(list(messages)), deepcopy(list(tools or [])),
                                  estimate_tokens(messages, tools or []), self.config.model)

    def _estimate_request(self, messages, tools) -> int:
        estimate = estimate_tokens(messages, tools)
        if self._usage_anchor is not None and self.last_usage_tokens is not None:
            prefix, old_tools, old_estimate, model = self._usage_anchor
            if model == self.config.model and tools == old_tools and list(messages[:len(prefix)]) == prefix:
                return max(0, self.last_usage_tokens + estimate - old_estimate)
        return estimate

    def input_budget(self) -> int | None:
        return self.budget.input_limit

    def should_compress(self, input_tokens: int) -> bool:
        return self.budget.should_compress(input_tokens)

    def _prepare_request(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        prepared = [dict(message) for message in messages]
        return prepared, self._estimate_request(prepared, tools)

    def prepare_messages(
        self,
        messages: list[dict[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        compression_client: Any,
    ) -> list[dict[str, Any]]:
        self.last_compression_event = None
        _, before_tokens = self._prepare_request(messages, tools)
        window = getattr(self.config, "context_window_tokens", None)
        if self.should_compress(before_tokens):
            self.compact(messages, tools, compression_client, "auto")
        prepared, total = self._prepare_request(messages, tools)
        if self.last_compression_event and self.should_compress(total):
            event = self.last_compression_event
            event.warning = (event.warning + "；" if event.warning else "") + "压缩后仍超过触发线；可能没有足够旧的原始内容"
        self.last_metrics = {
            "estimated_tokens": total,
            "window_tokens": window,
            "window_source": getattr(self.config, "context_window_source", "unknown"),
            "reserve": self.budget.reserve,
            "trigger": self.budget.trigger,
            "compression_triggered": self.last_compression_event is not None,
            "cut_index": (
                self.last_compression_event.cut_index
                if self.last_compression_event is not None
                else None
            ),
            "compaction_reason": (
                self.last_compression_event.reason
                if self.last_compression_event is not None
                else None
            ),
            "over_budget": self.budget.over_budget(total),
            "method": "estimated",
        }
        return prepared

    def compact(
        self,
        messages: list[dict[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        compression_client: Any,
        reason: str = "auto",
        keep_tokens: int | None = None,
    ) -> CompactionResult:
        """Run one compaction through the service and record the outcome.

        Automatic, manual and overflow recovery all land here, so they share
        the cut point, the checkpoint and the persisted record.
        """

        self.last_compression_event = None
        result = self.compactor.compact(
            messages,
            tools,
            compression_client,
            reason=reason,
            task=self.current_task,
            evidence=self.session_evidence,
            archive=self.session_archive,
            keep_tokens=keep_tokens,
        )
        self.compaction_failures = self.compactor.failures
        self.last_compression_event = result
        if result.compacted:
            # Compaction rewrites history, so the previous usage anchor is void.
            self._usage_anchor = None
        return result
