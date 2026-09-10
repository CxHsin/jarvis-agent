"""Runtime context bookkeeping for the Jarvis agent.

The model loop stays in :mod:`jarvis_agent`; this module owns the dynamic
information that is appended to or projected into each request.
"""

from __future__ import annotations

import html
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


CONTEXT_SUMMARY_OPEN = "<context_summary>"
CONTEXT_SUMMARY_CLOSE = "</context_summary>"
CONTEXT_COMPRESSED_MARKER = "[CONTEXT_COMPRESSED]"
CONTEXT_FALLBACK_MARKER = "[CONTEXT_COMPRESSED_FALLBACK]"


def estimate_tokens(messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]) -> int:
    """Conservatively estimate serialized request tokens without a tokenizer."""

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


class ContextManager:
    """Maintain session evidence, task state, status messages and compression."""

    def __init__(self, config: Any):
        self.config = config
        self.task_number = 0
        self.current_task: dict[str, Any] = {}
        self.session_evidence: list[dict[str, Any]] = []
        self.session_archive: list[dict[str, Any]] = []
        self._evidence_by_key: dict[str, str] = {}
        self._seen_call_keys: set[str] = set()
        self.last_usage_tokens: int | None = None
        self.last_usage_method: str | None = None
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
        self.session_archive.append(
            {
                "task": self.task_number,
                "call_id": call_id,
                "tool": name,
                "arguments": dict(arguments),
                "result": dict(result),
                "evidence_id": evidence_id,
                "compressed": False,
            }
        )

    def record_usage(self, usage: Mapping[str, Any] | None) -> None:
        prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, Mapping) else None
        if isinstance(prompt_tokens, int) and prompt_tokens >= 0:
            self.last_usage_tokens = prompt_tokens
            self.last_usage_method = "actual"

    def _budget_text(self, estimated_tokens: int) -> str:
        window = getattr(self.config, "context_window_tokens", None)
        threshold = getattr(self.config, "context_compression_threshold", 0.86)
        if not window:
            return f"tokens: estimated {estimated_tokens}; window: unknown; compression: disabled (window not configured)"
        ratio = estimated_tokens / window
        source = getattr(self.config, "context_window_source", "unknown")
        actual = ""
        if self.last_usage_tokens is not None:
            actual = f"; last_prompt_tokens: {self.last_usage_tokens} ({self.last_usage_method})"
        return (
            f"tokens: estimated {estimated_tokens}/{window} ({ratio:.1%}); "
            f"window_source: {source}; compression_threshold: {threshold:.0%}{actual}"
        )

    def _render_status(self, estimated_tokens: int) -> str:
        task = self.current_task or {"number": "?", "goal": "未命名任务", "tool_calls": 0, "repeated_calls": 0, "errors": [], "round": 0}
        goal = html.escape(str(task.get("goal", "")))
        lines = [
            "<agent_status>",
            f"Task: #{task.get('number')} {goal}",
            f"Round: {task.get('round', 0)}; tool_calls: {task.get('tool_calls', 0)}; repeated_calls: {task.get('repeated_calls', 0)}; errors: {len(task.get('errors', []))}",
            f"Budget: {html.escape(self._budget_text(estimated_tokens))}",
            "Evidence index:",
        ]
        visible = self.session_evidence[-20:]
        if not visible:
            lines.append("- (none)")
        else:
            for evidence in visible:
                refs = ", ".join(evidence["refs"])
                flags = []
                if evidence.get("repeated"):
                    flags.append("repeated")
                if evidence.get("compressed"):
                    flags.append("compressed")
                suffix = f" [{' '.join(flags)}]" if flags else ""
                lines.append(f"- {evidence['id']}: {evidence['tool']} -> {html.escape(refs)}{suffix}")
        if task.get("errors"):
            lines.append("Recent errors:")
            for error in task["errors"][-3:]:
                lines.append(f"- {html.escape(str(error))}")
        if self.last_compression_event:
            event = self.last_compression_event
            lines.append(
                f"Compression: {event.method}; {event.before_tokens} -> {event.after_tokens} tokens"
                + (f"; warning: {html.escape(event.warning)}" if event.warning else "")
            )
        lines.append("</agent_status>")
        return "\n".join(lines)

    def _request_with_status(self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        base = [dict(message) for message in messages]
        estimate = estimate_tokens(base, tools)
        status = self._render_status(estimate)
        total = estimate_tokens([*base, {"role": "user", "content": status}], tools)
        # One second pass makes the status percentage include its own size.
        status = self._render_status(total)
        total = estimate_tokens([*base, {"role": "user", "content": status}], tools)
        return [*base, {"role": "user", "content": status}], total

    def prepare_messages(
        self,
        messages: list[dict[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        compression_client: Any,
    ) -> list[dict[str, Any]]:
        self.last_compression_event = None
        _, before_tokens = self._request_with_status(messages, tools)
        window = getattr(self.config, "context_window_tokens", None)
        threshold = getattr(self.config, "context_compression_threshold", 0.86)
        if window and before_tokens / window >= threshold:
            self._compress(messages, tools, compression_client, before_tokens)
        prepared, total = self._request_with_status(messages, tools)
        self.last_metrics = {
            "estimated_tokens": total,
            "window_tokens": window,
            "window_source": getattr(self.config, "context_window_source", "unknown"),
            "threshold": threshold,
            "compression_triggered": self.last_compression_event is not None,
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

    def _compression_input(self, selected: list[tuple[int, Mapping[str, Any]]]) -> list[dict[str, Any]]:
        records = []
        archive_by_call = {item["call_id"]: item for item in self.session_archive}
        for index, message in selected:
            call_id = str(message.get("tool_call_id", f"message-{index}"))
            archive = archive_by_call.get(call_id, {})
            records.append(
                {
                    "message_index": index,
                    "tool_call_id": call_id,
                    "name": message.get("name", ""),
                    "content": message.get("content", ""),
                    "evidence_id": archive.get("evidence_id"),
                }
            )
        return records

    def _fallback_summary(self, records: list[dict[str, Any]]) -> str:
        max_chars = int(getattr(self.config, "context_summary_max_chars", 6000))
        lines = [CONTEXT_FALLBACK_MARKER, "语义摘要不可用；以下仅保留可核对的调用标识和有限预览。原始结果在会话归档中。"]
        for record in records:
            preview = str(record.get("content", "")).replace("\n", " ")[:240]
            lines.append(
                f"- {record['tool_call_id']} {record['name']} evidence={record.get('evidence_id')}: {preview}"
            )
        return "\n".join(lines)[:max_chars]

    def _compress(
        self,
        messages: list[dict[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        compression_client: Any,
        before_tokens: int,
    ) -> None:
        all_candidates = [
            (index, message)
            for index, message in enumerate(messages)
            if message.get("role") == "tool"
            and CONTEXT_COMPRESSED_MARKER not in str(message.get("content", ""))
            and CONTEXT_FALLBACK_MARKER not in str(message.get("content", ""))
        ]
        if not all_candidates:
            self.last_compression_event = CompressionEvent(
                before_tokens, before_tokens, "none", (), "没有可压缩的旧工具结果"
            )
            return

        window = getattr(self.config, "context_window_tokens", None)
        target = getattr(self.config, "context_compression_target", 0.65)
        candidates = all_candidates
        if window:
            target_tokens = int(window * target)
            selected: list[tuple[int, Mapping[str, Any]]] = []
            for candidate in all_candidates:
                selected.append(candidate)
                projected = [dict(message) for message in messages]
                selected_ids = {str(item[1].get("tool_call_id", f"message-{item[0]}")) for item in selected}
                first_id = str(selected[0][1].get("tool_call_id", f"message-{selected[0][0]}"))
                for index, message in enumerate(projected):
                    call_id = str(message.get("tool_call_id", f"message-{index}"))
                    if call_id not in selected_ids:
                        continue
                    if call_id == first_id:
                        message["content"] = CONTEXT_COMPRESSED_MARKER + "\n" + ("x" * min(getattr(self.config, "context_summary_max_chars", 6000), 6000))
                    else:
                        message["content"] = CONTEXT_COMPRESSED_MARKER + f"\n{call_id} included in {first_id}"
                projected_tokens = estimate_tokens(projected, tools)
                if projected_tokens <= target_tokens:
                    break
            candidates = selected

        records = self._compression_input(candidates)
        task = self.current_task or {"goal": "未命名任务"}
        prompt = (
            "请把以下 Jarvis 工具结果压缩成可供后续模型继续工作的事实摘要。"
            "保留任务相关事实、来源文件和行号、失败路径、未完成线索，并保留 evidence_id 和 tool_call_id。"
            "不要猜测原文没有提供的事实。只输出 <context_summary>...</context_summary>。\n\n"
            + json.dumps(
                {
                    "task_goal": task.get("goal", ""),
                    "evidence_index": self.session_evidence,
                    "tool_results": records,
                },
                ensure_ascii=False,
            )
        )
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
            summary = self._extract_summary(response.get("content") if isinstance(response, Mapping) else None)
            if not summary:
                warning = "压缩模型返回了无效摘要，已使用降级摘要"
        except Exception as exc:  # compression must never break the main task
            warning = f"压缩模型调用失败，已使用降级摘要: {exc}"
        if not summary:
            summary = self._fallback_summary(records)
            method = "fallback"
            self._compression_failures += 1
        else:
            self._compression_failures = 0

        first_index, first_message = candidates[0]
        first_call_id = str(first_message.get("tool_call_id", f"message-{first_index}"))
        first_message["content"] = f"{CONTEXT_COMPRESSED_MARKER}\n{summary}"
        for index, message in candidates[1:]:
            call_id = str(message.get("tool_call_id", f"message-{index}"))
            message["content"] = (
                f"{CONTEXT_COMPRESSED_MARKER}\n"
                f"tool result {call_id} 已纳入 {first_call_id} 的压缩摘要；原始内容在会话归档中。"
            )
        compressed_ids = tuple(str(message.get("tool_call_id", f"message-{index}")) for index, message in candidates)
        for evidence in self.session_evidence:
            if evidence["id"] in {record.get("evidence_id") for record in records}:
                evidence["compressed"] = True
        for archive in self.session_archive:
            if archive["call_id"] in compressed_ids:
                archive["compressed"] = True

        _, after_tokens = self._request_with_status(messages, tools)
        self.last_compression_event = CompressionEvent(
            before_tokens,
            after_tokens,
            method,
            compressed_ids,
            warning,
        )
