"""Runtime context bookkeeping for the Jarvis agent.

The model loop stays in :mod:`jarvis_agent`; this module owns the dynamic
information that is appended to or projected into each request.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from copy import deepcopy
from typing import Any, Mapping, Sequence

from cache_metrics import cache_usage


CONTEXT_SUMMARY_OPEN = "<context_summary>"
CONTEXT_SUMMARY_CLOSE = "</context_summary>"
CONTEXT_COMPRESSED_MARKER = "[CONTEXT_COMPRESSED]"
CONTEXT_FALLBACK_MARKER = "[CONTEXT_COMPRESSED_FALLBACK]"
CONTEXT_RECOVERED_MARKER = "[CONTEXT_RECOVERED]"


def estimate_tokens(messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]) -> int:
    """Roughly estimate serialized request tokens; this is not an upper bound."""

    payload = json.dumps(
        {"messages": list(messages), "tools": list(tools)},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    # Four UTF-8 bytes is a deliberately rough cross-provider estimate.  It
    # is more conservative for CJK text than counting Python characters.
    return max(1, math.ceil(len(payload.encode("utf-8")) / 4))


@dataclass
class CompressionEvent:
    before_tokens: int
    after_tokens: int
    method: str
    compressed_call_ids: tuple[str, ...]
    warning: str | None = None
    cut_index: int | None = None


class ContextManager:
    """Maintain session evidence, task state, status messages and compression."""

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
        "_compression_failures",
    )

    def __init__(self, config: Any, recorder: Any = None):
        self.config = config
        self.recorder = recorder
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
        self.last_compression_event: CompressionEvent | None = None
        self._compression_failures = 0

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

        return {name: deepcopy(getattr(self, name)) for name in self._SNAPSHOT_FIELDS}

    def restore(self, state: Mapping[str, Any]) -> None:
        """Undo a failed request back to a snapshot taken by :meth:`snapshot`."""

        for name in self._SNAPSHOT_FIELDS:
            setattr(self, name, deepcopy(state[name]))

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
        if name == "read_file" and result.get("path"):
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
        window = getattr(self.config, "context_window_tokens", None)
        if window is None:
            return None
        budget = window - self.config.max_output_tokens - math.ceil(window * self.config.context_safety_margin)
        return min(budget, getattr(self.config, "model_max_input_tokens", None) or budget)

    def should_compress(self, input_tokens: int) -> bool:
        window = getattr(self.config, "context_window_tokens", None)
        if not window:
            return False
        reserved = self.config.max_output_tokens + math.ceil(window * self.config.context_safety_margin)
        return (input_tokens + reserved > window * self.config.context_compression_threshold
                or input_tokens > self.input_budget())

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
        threshold = getattr(self.config, "context_compression_threshold", 0.90)
        if self.should_compress(before_tokens):
            self._compress(messages, tools, compression_client, before_tokens)
        prepared, total = self._prepare_request(messages, tools)
        budget = self.input_budget()
        if self.last_compression_event and self.should_compress(total):
            event = self.last_compression_event
            event.warning = (event.warning + "；" if event.warning else "") + "压缩后仍超过触发线；可能没有足够旧的原始内容"
        self.last_metrics = {
            "estimated_tokens": total,
            "window_tokens": window,
            "window_source": getattr(self.config, "context_window_source", "unknown"),
            "threshold": threshold,
            "compression_triggered": self.last_compression_event is not None,
            "cut_index": (
                self.last_compression_event.cut_index
                if self.last_compression_event is not None
                else None
            ),
            "over_budget": budget is not None and total > budget,
            "method": "estimated",
        }
        return prepared

    @staticmethod
    def _extract_summary(content: Any) -> str | None:
        if not isinstance(content, str):
            return None
        start = content.find(CONTEXT_SUMMARY_OPEN)
        end = content.find(CONTEXT_SUMMARY_CLOSE, start + len(CONTEXT_SUMMARY_OPEN))
        if start < 0 or end < 0:
            return None
        summary = content[start + len(CONTEXT_SUMMARY_OPEN) : end].strip()
        return summary or None

    @staticmethod
    def _boundary_kind(message: Mapping[str, Any]) -> str | None:
        role = message.get("role")
        if role == "user":
            return "task"
        if role == "assistant":
            return "round"
        if role == "tool":
            return "tool"
        return None

    def _find_cut_point(self, messages: Sequence[Mapping[str, Any]]) -> int | None:
        """Return the first index of the kept suffix, snapped to a safe boundary."""

        keep = int(getattr(self.config, "context_keep_recent_tokens", 0))
        if keep <= 0 or len(messages) <= 1:
            return None
        boundaries = [
            index
            for index in range(1, len(messages))
            if self._boundary_kind(messages[index]) in {"task", "round"}
        ]
        if not boundaries:
            return None
        accumulated = 0
        for index in range(len(messages) - 1, 0, -1):
            accumulated += estimate_tokens([messages[index]], [])
            if accumulated >= keep:
                for boundary in reversed(boundaries):
                    if boundary <= index:
                        return boundary
        return boundaries[0]

    def _retired_span(
        self,
        messages: Sequence[Mapping[str, Any]],
        cut_index: int,
    ) -> tuple[str | None, list[Mapping[str, Any]]]:
        retired = list(messages[1:cut_index])
        previous_summary: str | None = None
        raw = retired
        first = retired[0] if retired else None
        if (
            first is not None
            and first.get("role") == "user"
            and str(first.get("content", "")).startswith(CONTEXT_COMPRESSED_MARKER)
        ):
            previous_summary = self._extract_summary(first.get("content"))
            raw = retired[1:]
        return previous_summary, raw

    @staticmethod
    def _serialize_message(message: Mapping[str, Any]) -> str:
        role = message.get("role")
        content = message.get("content")
        if role == "user":
            return f"[User]: {content}"
        if role == "assistant":
            lines: list[str] = []
            if content:
                lines.append(f"[Assistant]: {content}")
            for call in message.get("tool_calls") or []:
                if not isinstance(call, Mapping):
                    continue
                function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
                name = function.get("name", "")
                arguments = function.get("arguments", "")
                lines.append(f"[Assistant tool call {call.get('id', '')}]: {name}({arguments})")
            return "\n".join(lines) if lines else "[Assistant]: (空)"
        if role == "tool":
            return f"[Tool result {message.get('tool_call_id', '')}]: {content}"
        return f"[{role}]: {content}"

    def _compression_prompt(
        self,
        raw_messages: Sequence[Mapping[str, Any]],
        previous_summary: str | None,
    ) -> str:
        task = self.current_task or {"goal": "未命名任务"}
        conversation = "\n".join(
            self._serialize_message(message) for message in raw_messages
        )
        parts = [
            "你是 Jarvis 的上下文压缩器。请把下面的对话压缩成结构化摘要，用于后续继续工作。",
            "只输出 <context_summary>...</context_summary>；不要续写对话，不要回答对话里的问题。",
            "摘要必须包含：Goal、Progress（Done/In Progress/Blocked）、Key Decisions、Next Steps、Critical Context、Sources。",
            "Sources 只写 path:line 或 evidence_id，保留可核对来源；不要猜测原文没有提供的事实。",
            "",
            f"任务目标: {task.get('goal', '')}",
        ]
        if previous_summary:
            parts += [
                "",
                "上一次摘要（在此基础上累积，而不是重复描述已经解决的历史）:",
                previous_summary,
            ]
        parts += [
            "",
            "需要压缩的对话:",
            conversation,
            "",
            "证据索引:",
            json.dumps(self.session_evidence, ensure_ascii=False),
        ]
        return "\n".join(parts)

    def _fallback_summary(self, raw_messages: Sequence[Mapping[str, Any]]) -> str:
        max_chars = int(getattr(self.config, "context_summary_max_chars", 6000))
        lines = [
            CONTEXT_FALLBACK_MARKER,
            "语义摘要不可用；以下保留可核对的消息标识和有限预览。原始结果在会话归档中。",
        ]
        for message in raw_messages:
            role = message.get("role")
            if role == "tool":
                preview = str(message.get("content", "")).replace("\n", " ")[:240]
                lines.append(f"- tool {message.get('tool_call_id', '')}: {preview}")
            elif role == "user":
                preview = str(message.get("content", "")).replace("\n", " ")[:160]
                lines.append(f"- user: {preview}")
            elif role == "assistant":
                text = message.get("content")
                if text:
                    lines.append(f"- assistant: {str(text).replace(chr(10), ' ')[:160]}")
                for call in message.get("tool_calls") or []:
                    if not isinstance(call, Mapping):
                        continue
                    function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
                    lines.append(
                        f"- assistant tool_call {call.get('id', '')}: "
                        f"{function.get('name', '')} {function.get('arguments', '')}"
                    )
        return "\n".join(lines)[:max_chars]

    def _compress(
        self,
        messages: list[dict[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        compression_client: Any,
        before_tokens: int,
    ) -> None:
        cut_index = self._find_cut_point(messages)
        if cut_index is None:
            self.last_compression_event = CompressionEvent(
                before_tokens, before_tokens, "none", (), "没有可压缩的旧内容"
            )
            return

        previous_summary, raw_messages = self._retired_span(messages, cut_index)
        if not raw_messages:
            self.last_compression_event = CompressionEvent(
                before_tokens, before_tokens, "none", (), "没有新的旧内容可归档"
            )
            return

        failure_limit = int(getattr(self.config, "context_compaction_failure_limit", 3))
        if self._compression_failures >= failure_limit:
            self.last_compression_event = CompressionEvent(
                before_tokens,
                before_tokens,
                "none",
                (),
                f"连续 {self._compression_failures} 次摘要失败，已触发熔断器",
            )
            return

        prompt = self._compression_prompt(raw_messages, previous_summary)
        summary: str | None = None
        warning: str | None = None
        method = "model"
        try:
            response = compression_client.complete(
                [
                    {
                        "role": "system",
                        "content": "你是 Jarvis 的上下文压缩器。输出必须是严格的 context_summary 块。",
                    },
                    {"role": "user", "content": prompt},
                ],
                [],
                "none",
            )
            summary = self._extract_summary(
                response.get("content") if isinstance(response, Mapping) else None
            )
            if not summary:
                warning = "压缩模型返回了无效摘要，已使用降级摘要"
        except Exception as exc:  # compression must never break the main task
            warning = f"压缩模型调用失败，已使用降级摘要: {exc}"
        if not summary:
            summary = self._fallback_summary(raw_messages)
            method = "fallback"
            self._compression_failures += 1
        else:
            self._compression_failures = 0

        summary = summary[: int(getattr(self.config, "context_summary_max_chars", 6000))]
        compressed_call_ids = tuple(
            str(message.get("tool_call_id", ""))
            for message in raw_messages
            if message.get("role") == "tool" and message.get("tool_call_id")
        )

        self._usage_anchor = None
        checkpoint_content = (
            f"{CONTEXT_COMPRESSED_MARKER}\n<context_summary>{summary}</context_summary>"
        )
        kept_suffix = [dict(message) for message in messages[cut_index:]]
        messages[:] = (
            [dict(messages[0])]
            + [{"role": "user", "content": checkpoint_content}]
            + kept_suffix
        )
        self._record_compact(cut_index, checkpoint_content, method, compressed_call_ids)

        archive_by_call = {item["call_id"]: item for item in self.session_archive}
        compressed_evidence_ids: set[str] = set()
        for call_id in compressed_call_ids:
            archive = archive_by_call.get(call_id)
            if archive is not None and archive.get("evidence_id"):
                compressed_evidence_ids.add(archive["evidence_id"])
        for archive in self.session_archive:
            if archive["call_id"] in compressed_call_ids:
                archive["compressed"] = True
        for evidence in self.session_evidence:
            if evidence["id"] in compressed_evidence_ids:
                evidence["compressed"] = True

        _, after_tokens = self._prepare_request(messages, tools)
        self.last_compression_event = CompressionEvent(
            before_tokens,
            after_tokens,
            method,
            compressed_call_ids,
            warning,
            cut_index,
        )

    def _record_compact(
        self,
        kept_from: int,
        content: str,
        method: str,
        compressed_call_ids: tuple[str, ...],
    ) -> None:
        if self.recorder is None:
            return
        self.recorder.record_compact(kept_from, content, method, compressed_call_ids)
