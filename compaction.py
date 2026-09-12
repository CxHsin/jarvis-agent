"""Compaction as a service: one entry point, three triggers, one effect.

Automatic compression, the explicit ``/compact`` command and the overflow
recovery path all run through :meth:`CompactionService.compact`.  Only the
reason and the way the outcome is reported may differ; the cut point, the
cumulative checkpoint, the archive marks and the persisted record are shared.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from context_budget import estimate_tokens


CONTEXT_SUMMARY_OPEN = "<context_summary>"
CONTEXT_SUMMARY_CLOSE = "</context_summary>"
CONTEXT_COMPRESSED_MARKER = "[CONTEXT_COMPRESSED]"
CONTEXT_FALLBACK_MARKER = "[CONTEXT_COMPRESSED_FALLBACK]"

AUTO = "auto"
MANUAL = "manual"
OVERFLOW = "overflow"
REASONS = (AUTO, MANUAL, OVERFLOW)


@dataclass
class CompactionResult:
    """What one compaction produced, for reporting and for tests."""

    reason: str
    before_tokens: int
    after_tokens: int
    method: str
    compressed_call_ids: tuple[str, ...] = ()
    warning: str | None = None
    cut_index: int | None = None

    @property
    def compacted(self) -> bool:
        return self.method != "none"


class CompactionService:
    """Retire older turns into one cumulative checkpoint."""

    def __init__(self, config: Any, recorder: Any = None,
                 estimator: Callable[[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]], int] = estimate_tokens):
        self.config = config
        self.recorder = recorder
        self.estimator = estimator
        self.failures = 0

    # -- public entry point -------------------------------------------------

    def compact(
        self,
        messages: list[dict[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        client: Any,
        reason: str = AUTO,
        task: Mapping[str, Any] | None = None,
        evidence: Sequence[Mapping[str, Any]] = (),
        archive: Sequence[dict[str, Any]] = (),
    ) -> CompactionResult:
        """Rewrite ``messages`` in place, returning what the compaction did."""

        before_tokens = self.estimator(messages, tools)
        cut_index = self._find_cut_point(messages)
        if cut_index is None:
            return self._no_op(reason, before_tokens, "没有可压缩的旧内容")

        previous_summary, raw_messages = self._retired_span(messages, cut_index)
        if not raw_messages:
            return self._no_op(reason, before_tokens, "没有新的旧内容可归档")

        failure_limit = int(getattr(self.config, "context_compaction_failure_limit", 3))
        if self.failures >= failure_limit:
            return self._no_op(
                reason, before_tokens, f"连续 {self.failures} 次摘要失败，已触发熔断器"
            )

        summary, method, warning = self._summarize(raw_messages, previous_summary, client, task, evidence)
        summary = summary[: int(getattr(self.config, "context_summary_max_chars", 6000))]
        compressed_call_ids = tuple(
            str(message.get("tool_call_id", ""))
            for message in raw_messages
            if message.get("role") == "tool" and message.get("tool_call_id")
        )

        checkpoint_content = (
            f"{CONTEXT_COMPRESSED_MARKER}\n<context_summary>{summary}</context_summary>"
        )
        kept_suffix = [dict(message) for message in messages[cut_index:]]
        messages[:] = (
            [dict(messages[0])]
            + [{"role": "user", "content": checkpoint_content}]
            + kept_suffix
        )
        self._record(reason, cut_index, checkpoint_content, method, compressed_call_ids)
        self._mark_compressed(archive, evidence, compressed_call_ids)

        return CompactionResult(
            reason=reason,
            before_tokens=before_tokens,
            after_tokens=self.estimator(messages, tools),
            method=method,
            compressed_call_ids=compressed_call_ids,
            warning=warning,
            cut_index=cut_index,
        )

    # -- cut point ----------------------------------------------------------

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
            accumulated += self.estimator([messages[index]], [])
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

    # -- summary ------------------------------------------------------------

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
                lines.append(
                    f"[Assistant tool call {call.get('id', '')}]: "
                    f"{function.get('name', '')}({function.get('arguments', '')})"
                )
            return "\n".join(lines) if lines else "[Assistant]: (空)"
        if role == "tool":
            return f"[Tool result {message.get('tool_call_id', '')}]: {content}"
        return f"[{role}]: {content}"

    def _compression_prompt(
        self,
        raw_messages: Sequence[Mapping[str, Any]],
        previous_summary: str | None,
        task: Mapping[str, Any] | None,
        evidence: Sequence[Mapping[str, Any]],
    ) -> str:
        current = task or {"goal": "未命名任务"}
        conversation = "\n".join(self._serialize_message(message) for message in raw_messages)
        parts = [
            "你是 Jarvis 的上下文压缩器。请把下面的对话压缩成结构化摘要，用于后续继续工作。",
            "只输出 <context_summary>...</context_summary>；不要续写对话，不要回答对话里的问题。",
            "摘要必须包含：Goal、Progress（Done/In Progress/Blocked）、Key Decisions、Next Steps、Critical Context、Sources。",
            "Sources 只写 path:line 或 evidence_id，保留可核对来源；不要猜测原文没有提供的事实。",
            "",
            f"任务目标: {current.get('goal', '')}",
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
            json.dumps(list(evidence), ensure_ascii=False),
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

    def _summarize(
        self,
        raw_messages: Sequence[Mapping[str, Any]],
        previous_summary: str | None,
        client: Any,
        task: Mapping[str, Any] | None,
        evidence: Sequence[Mapping[str, Any]],
    ) -> tuple[str, str, str | None]:
        prompt = self._compression_prompt(raw_messages, previous_summary, task, evidence)
        summary: str | None = None
        warning: str | None = None
        method = "model"
        try:
            response = client.complete(
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
            self.failures += 1
        else:
            self.failures = 0
        return summary, method, warning

    # -- side effects -------------------------------------------------------

    def _no_op(self, reason: str, before_tokens: int, warning: str) -> CompactionResult:
        return CompactionResult(reason, before_tokens, before_tokens, "none", (), warning)

    def _record(
        self,
        reason: str,
        kept_from: int,
        content: str,
        method: str,
        compressed_call_ids: tuple[str, ...],
    ) -> None:
        if self.recorder is None:
            return
        self.recorder.record_compact(kept_from, content, method, compressed_call_ids, reason)

    @staticmethod
    def _mark_compressed(
        archive: Sequence[dict[str, Any]],
        evidence: Sequence[Mapping[str, Any]],
        compressed_call_ids: tuple[str, ...],
    ) -> None:
        compressed = set(compressed_call_ids)
        archive_by_call = {item.get("call_id"): item for item in archive}
        evidence_ids = {
            archive_by_call[call_id]["evidence_id"]
            for call_id in compressed
            if call_id in archive_by_call and archive_by_call[call_id].get("evidence_id")
        }
        for entry in archive:
            if entry.get("call_id") in compressed:
                entry["compressed"] = True
        for item in evidence:
            if item.get("id") in evidence_ids:
                item["compressed"] = True
