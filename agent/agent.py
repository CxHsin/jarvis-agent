"""Agent task execution and model-tool loop."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Callable, Mapping

from application import Application
from configuration import Config
from context.context_manager import CONTEXT_RECOVERED_MARKER, ContextManager
from context.compaction import CompactionResult, MANUAL, OVERFLOW, is_overflow_error
from models.cache_metrics import MeasuredClient, UsageLedger
from models.model_client import ChatCompletionsClient, ModelRequestError
from session.session_store import SessionContents, SessionStore
from tools.definitions import TOOL_DEFINITIONS
from tools.workspace import Workspace, WorkspaceError
from tools.tool_runtime import (ToolRegistry, ToolRuntime, PermissionPolicy, ProviderSession,
                                ToolProviderAdapter, ProviderLoadError, failure)

ToolFunction = Callable[..., dict[str, Any]]


class Agent:
    def __init__(
        self,
        config: Config,
        client: ChatCompletionsClient | Any | None = None,
        compression_client: ChatCompletionsClient | Any | None = None,
        store: SessionStore | None = None,
        resume: str | None = None,
        tool_runtime: ToolRuntime | None = None,
        permission_policy: PermissionPolicy | None = None,
        confirm_tool: Callable | None = None,
        native_loader: Callable | None = None,
        application: Application | None = None,
    ):
        supplied_runtime = tool_runtime is not None
        self._owns_application = application is None
        self.application = application or Application(config, client=client,
            compression_client=compression_client)
        self.store = store
        try:
            if self.store is None and resume is not None:
                # Acquire the session lock before model discovery or background work.
                self.store = SessionStore.resume(config, resume or None)
            self.application.prepare()
            self.config = self.application.config
            if self.store is None:
                self.store = SessionStore.create(self.config)
        except BaseException:
            if self.store is not None:
                self.store.close()
            if self._owns_application:
                self.application.close()
            raise
        try:
            self.workspace = Workspace(self.config)
            self.tool_registry = tool_runtime.registry if tool_runtime is not None else ToolRegistry()
            self.usage_ledger = UsageLedger()
            self.compression_client = MeasuredClient(self.application.compression_client, self.usage_ledger,
                                                    "压缩模型", config.compression_model or config.model)
            self.client = MeasuredClient(self.application.client, self.usage_ledger, "主模型", config.model)
            self._audit_events = []
            self.messages: list[dict[str, Any]] = [{"role": "system", "content": self._system_prompt()}]
            self.context = ContextManager(self.config, recorder=self.store)
            self.tool_functions: dict[str, ToolFunction] = {
                "read": self.workspace.read, "edit": self.workspace.edit, "bash": self.workspace.bash,
                "tool_search": self._search_tools,
                "list_directory": self.workspace.list_directory, "search_file_content": self.workspace.search_file_content, "read_file": self.workspace.read_file,
            }
            self.tool_runtime = tool_runtime or ToolRuntime(self.tool_registry,
                stable=tuple((name, "1") for name in
                             ("read", "edit", "bash", "tool_search", "list_directory")))
            self.tool_runtime.install_tools(TOOL_DEFINITIONS, {
                **self.tool_functions, "edit": self._contextual_edit, "bash": self._contextual_bash,
            }, defaults=not supplied_runtime)
            policy = permission_policy
            if policy is None and not supplied_runtime:
                policy = PermissionPolicy(self.config.tool_permission_mode,
                                          max_timeout=self.config.tool_max_timeout)
            self.tool_runtime.configure(policy=policy, confirm=confirm_tool,
                                        recorder=self._record_audit, policy_recorder=self._policy_event)
            self.tool_runtime.attach_workspace(self.workspace)
            self.provider_session = ProviderSession(self.config.provider_tool_mode)
            self.provider_adapter = ToolProviderAdapter(self.tool_runtime, self.provider_session)
            self.native_loader = native_loader
            for registered in self.tool_runtime.stable_tools:
                name = str(registered.metadata.schema.get("name", registered.metadata.tool_id))
                self.tool_functions.setdefault(name, registered.handler)
            self._runtime_task_id = "0"
            self._restore_session()
            self.application.session_started(self)
        except BaseException:
            self.application.session_closed(self)
            self.store.close()
            if self._owns_application:
                self.application.close()
            raise

    def _save_runtime(self, audit=None):
        if self.store:
            self.store.record_runtime(dict(self.tool_runtime.snapshot(), provider=self.provider_session.snapshot()),
                                      audit=audit)

    def _record_audit(self, event):
        self._audit_events.append(deepcopy(event))
        self.store.record_tool_audit(event)

    def _policy_event(self, event):
        self._save_runtime(audit=event)
        self._audit_events.append(deepcopy(event))

    def _search_tools(self, query="", limit=8):
        result = self.tool_runtime.discover(self._runtime_task_id, query, limit)
        self._save_runtime()
        return result

    def _ensure_dynamic_definitions(self):
        """Restore only active definitions when compaction retires their search result."""
        visible = set()
        for message in self.messages:
            if message.get("role") == "tool" and message.get("name") == "tool_search":
                try:
                    items = json.loads(message.get("content", "{}")).get("tools", [])
                    visible.update(item["schema_fingerprint"] for item in items if "schema" in item)
                except (ValueError, TypeError, KeyError, AttributeError):
                    pass
            elif message.get("role") == "system" and str(message.get("content", "")).startswith("Active tool definitions:\n"):
                items = json.loads(message["content"].split("\n", 1)[1])
                visible.update(item["schema_fingerprint"] for item in items)
        missing = [item for item in self.tool_runtime.active_definitions(self._runtime_task_id)
                   if item["schema_fingerprint"] not in visible and item["tool_id"] not in self.tool_functions]
        if not missing:
            return False
        self._append_message({"role": "system", "content": "Active tool definitions:\n" +
                              json.dumps(missing, ensure_ascii=False, sort_keys=True, separators=(",", ":"))})
        return True

    def _contextual_edit(self, context, **arguments):
        return self.workspace.edit(**arguments, execution_context=context)

    def _contextual_bash(self, context, **arguments):
        return self.workspace.bash(**arguments, execution_context=context)

    def _restore_session(self) -> None:
        """Load persisted history into this process, repairing interrupted rounds."""

        contents: SessionContents = self.store.load()
        self.tool_runtime.restore(contents.runtime)
        if contents.runtime.get("provider"):
            self.provider_session = ProviderSession(**contents.runtime["provider"])
            self.provider_adapter.session = self.provider_session
        for warning in contents.warnings:
            print(f"[会话恢复] {warning}")
        if not contents.messages:
            print(f"[会话] 新会话 {self.store.session_id}")
            return
        self.messages.extend(contents.messages)
        self.context.task_number = contents.task_number
        self.context.restore_session(contents.archive, contents.compressed_call_ids)
        restored = len(contents.messages)
        self._repair_interrupted_calls()
        last_user = next(
            (
                str(message.get("content", "")).replace("\n", " ")
                for message in reversed(self.messages)
                if message.get("role") == "user"
            ),
            "",
        )
        summary = (last_user[:60] + "…") if len(last_user) > 60 else last_user
        print(f"[会话恢复] {self.store.session_id}；消息 {restored} 条" + (f"；最后输入: {summary}" if summary else ""))

    def _repair_interrupted_calls(self) -> None:
        """Give unanswered tool calls a placeholder result instead of re-running them."""

        answered = {
            str(message.get("tool_call_id"))
            for message in self.messages
            if message.get("role") == "tool" and message.get("tool_call_id") is not None
        }
        missing: list[tuple[str, str]] = []
        for message in self.messages:
            if message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or []:
                if not isinstance(call, Mapping):
                    continue
                call_id = str(call.get("id") or "")
                if not call_id or call_id in answered:
                    continue
                function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
                missing.append((call_id, str(function.get("name", ""))))
                answered.add(call_id)
        for call_id, name in missing:
            payload = {"ok": False, "recovered": True, "error": "工具结果未完整记录，执行状态未知；恢复不会重放该调用。"}
            content = f"{CONTEXT_RECOVERED_MARKER}\n{json.dumps(payload, ensure_ascii=False)}"
            self._append_message({"role": "tool", "tool_call_id": call_id, "name": name, "content": content})
            print(f"[会话恢复] 工具调用 {name or '?'} ({call_id}) 缺少结果，已补写中断占位。")

    def _append_message(self, message: dict[str, Any]) -> None:
        if self.store is not None:
            self.store.record_message(message)
        self.messages.append(message)

    def close(self) -> None:
        if self.store is not None:
            self.store.close()
            self.store = None
        self.context.recorder = None
        self.application.session_closed(self)
        if self._owns_application:
            self.application.close()

    def _system_prompt(self) -> str:
        extensions = ", ".join(self.config.text_extensions)
        return (
            self.config.system_prompt + "\n" +
            f"默认工作区是 {self.config.root_dir}；read/read_file 可读取工作区外的文本文件，相对路径以工作区为基准；文本扩展名包括 {extensions}。\n"
            "tool_search 返回并激活工具定义，可在本次任务后续轮次调用；旧任务的定义不代表当前可调用。"
            "同批调用可用 _depends_on 指定前置调用 ID；参数值可用 "
            '{"$result":{"call_id":"前置ID","path":["字段"]}} 引用前置结果。'
        )

    @staticmethod
    def _tool_calls(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        calls = message.get("tool_calls")
        return list(calls) if isinstance(calls, list) else []

    @staticmethod
    def _normalise_args(raw):
        try:
            return ToolRuntime.normalise_args(raw)
        except ValueError as exc:
            raise WorkspaceError(str(exc)) from exc

    def _execute_tool(self, name, arguments, **kwargs):
        return self.tool_runtime.execute_model_call(self._runtime_task_id, name, arguments, **kwargs)

    def _complete_with_tools(self, messages, tools, choice):
        def native():
            if self.native_loader is None:
                raise ProviderLoadError("native deferred adapter is unsupported by this client")
            return self.native_loader(self.client, deepcopy(messages), deepcopy(tools),
                                      self.tool_runtime.active_definitions(self._runtime_task_id), choice)
        try:
            return self.provider_adapter.load(self._runtime_task_id, native,
                                              emulated_loader=lambda: self.client.complete(messages, tools, choice))
        except ProviderLoadError as exc:
            raise ModelRequestError(str(exc)) from exc
        except Exception as exc:
            raise ModelRequestError(str(exc)) from exc
        finally:
            self._save_runtime()

    def _tool_result_for_display(self, name: str, result: Mapping[str, Any]) -> str:
        """Render a compact terminal preview without changing the model result."""
        if self.config.verbose_tool_output:
            return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

        if not result.get("ok"):
            preview = f"tool={name} ok=false"
            if "exit_code" in result:
                preview += f" exit_code={result['exit_code']}"
            if result.get("output"):
                preview += f" output={str(result['output']).strip()}"
            preview += f" error={result.get('error', '工具调用失败')}"
        elif name == "list_directory":
            entries = result.get("entries", [])
            names = [str(entry.get("name", "?")) for entry in entries[:5] if isinstance(entry, Mapping)]
            suffix = "..." if len(entries) > 5 else ""
            preview = (
                f"ok=true path={result.get('path', '?')} entries={len(entries)}"
                f" [{', '.join(names)}{suffix}]"
            )
        elif name == "search_file_content":
            matches = result.get("matches", [])
            references = [
                f"{item.get('path', '?')}:{item.get('line', '?')}"
                for item in matches[:5]
                if isinstance(item, Mapping)
            ]
            suffix = "..." if len(matches) > 5 else ""
            preview = (
                f"ok=true query={result.get('query', '')!r} matches={len(matches)}"
                f" truncated={bool(result.get('truncated'))}"
                f" [{', '.join(references)}{suffix}]"
            )
        elif name == "read_file":
            content = str(result.get("content", "")).replace("\n", " ")
            preview = (
                f"ok=true path={result.get('path', '?')} "
                f"lines={result.get('start_line', '?')}-{result.get('end_line', '?')} "
                f"truncated={bool(result.get('truncated'))} preview={content}"
            )
        else:
            preview = json.dumps(result, ensure_ascii=False, separators=(",", ":"))

        limit = self.config.tool_output_preview_chars
        if len(preview) > limit:
            preview = preview[:limit].rstrip() + "..."
        return preview

    @staticmethod
    def _assistant_message(message: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
        if "reasoning_content" in message:
            result["reasoning_content"] = message["reasoning_content"]
        if message.get("tool_calls"):
            result["tool_calls"] = message["tool_calls"]
        return result

    def run_request(self, user_text: str) -> str | None:
        self.messages[0] = {"role": "system", "content": self._system_prompt()}
        task_system_message = dict(self.messages[0])
        request_offset = self.store.mark() if self.store is not None else None
        message_snapshot = deepcopy(self.messages)
        context_snapshot = self.context.snapshot()
        runtime_snapshot = self.tool_runtime.snapshot()
        audit_start = self.tool_runtime.audit_cursor()
        audit_event_start = len(self._audit_events)
        self.context.begin_task(user_text)
        recent = self.context.select_messages(self.store, self.messages)
        if recent != self.messages[1:]:
            self.messages[1:] = recent
        self._runtime_task_id = f"{self.store.session_id}:{self.context.task_number}"
        self.tool_runtime.begin_task(self._runtime_task_id)
        self._save_runtime()
        self.usage_ledger.reset()
        self._append_message({"role": "user", "content": user_text})
        active_calls: list[Mapping[str, Any]] = []
        handled_call_indexes: set[int] = set()
        task_status = "interrupted"
        context_valid = True
        try:
            for round_number in range(1, self.config.max_rounds + 1):
                final_round = round_number == self.config.max_rounds
                if final_round:
                    self.messages[0] = {
                        **task_system_message,
                        "content": task_system_message["content"] + (
                            "\n\n当前是本次任务的最后一轮。请结合已有上下文和工具结果，直接回答用户原始请求。"
                            "本轮不能再使用工具；不要输出工具调用、调用标记（如 DSML）或继续执行的计划。"
                            "给出已有证据支持的答案，区分已确认的结果、尚未完成的部分和无法确认的信息；"
                            "信息不足时明确说明，不要编造结果或把失败的操作说成成功。"
                        ),
                    }
                self.context.set_round(round_number)
                print(f"\n[第 {round_number}/{self.config.max_rounds} 轮] 请求模型" + ("（收尾）" if final_round else ""))
                tools = [] if final_round else self.tool_runtime.schemas(self._runtime_task_id)
                overflow_retried = False
                while True:
                    request_messages = self.context.prepare_messages(self.messages, tools, self.compression_client)
                    if self._ensure_dynamic_definitions():
                        # Account for restored schemas without repeatedly compacting them away.
                        request_messages, total = self.context._prepare_request(self.messages, tools)
                        self.context.last_metrics.update(estimated_tokens=total,
                                                         over_budget=self.context.budget.over_budget(total))
                    metrics = self.context.last_metrics
                    print(
                        f"[上下文] 估算 {metrics.get('estimated_tokens', '?')} tokens"
                        + (
                            f" / 窗口 {metrics['window_tokens']} ({metrics.get('window_source', 'unknown')})"
                            if metrics.get("window_tokens")
                            else " / 窗口未配置"
                        )
                    )
                    if self.context.last_compression_event:
                        event = self.context.last_compression_event
                        print(
                            f"[上下文压缩/{event.reason}] {event.method}: "
                            f"{event.before_tokens} -> {event.after_tokens} tokens"
                            + (f"；{event.warning}" if event.warning else "")
                        )
                    if metrics.get("over_budget"):
                        raise ModelRequestError("压缩后输入仍超过可用输入预算；请缩小本次输入或开启新会话。")
                    try:
                        message = self._complete_with_tools(
                            request_messages,
                            tools,
                            "none" if final_round else "auto",
                        )
                        break
                    except ModelRequestError as exc:
                        if overflow_retried or not is_overflow_error(exc):
                            raise
                        overflow_retried = True
                        print("[溢出恢复] 服务端报告上下文超限，压缩后重试一次。")
                        recovered = self.context.compact(self.messages, tools, self.compression_client, OVERFLOW)
                        if not recovered.compacted:
                            raise
                self.context.record_usage(self.client.last_usage, request_messages, tools)
                assistant = self._assistant_message(message)
                self._append_message(assistant)
                calls = self._tool_calls(message)
                active_calls = calls
                handled_call_indexes = set()
                if not calls:
                    answer = message.get("content") or "模型没有返回文字回答。"
                    print(f"\nJarvis> {answer}")
                    task_status = "completed"
                    return str(answer)
                if final_round:
                    for call in calls:
                        function = call.get("function") or {}
                        self._append_message({"role": "tool", "tool_call_id": call.get("id", ""),
                                              "name": function.get("name", ""),
                                              "content": json.dumps(failure("round_limit"))})
                    print("已达到轮次上限，本次请求未完成。")
                    return None
                def record_result(item, result):
                    result_text = json.dumps(result, ensure_ascii=False)
                    print(f"[工具结果] {self._tool_result_for_display(item.tool_id, result)}")
                    self._append_message({"role": "tool", "tool_call_id": item.call_id,
                                          "name": item.tool_id, "content": result_text})
                    self.context.record_tool_result(item.tool_id, item.arguments, result, item.call_id)
                    handled_call_indexes.update(i for i, c in enumerate(calls) if c.get("id") == item.call_id)
                cancelled = self.tool_runtime.execute_batch(self._runtime_task_id, calls, record_result)
                if cancelled:
                    task_status = "cancelled"
                    active_calls = []
                    print("已取消当前请求。工具结果已保留。")
                    return None
                active_calls = []
            print("已达到轮次上限，本次请求未完成。")
            return None
        except KeyboardInterrupt:
            task_status = "cancelled"
            for call_index, call in enumerate(active_calls):
                if call_index in handled_call_indexes:
                    continue
                function_data = call.get("function", {}) if isinstance(call, Mapping) else {}
                name = function_data.get("name", "")
                call_id = call.get("id", "")
                result_text = json.dumps(
                    {"ok": False, "cancelled": True, "error": "工具调用因用户取消而未执行。"},
                    ensure_ascii=False,
                )
                self._append_message({"role": "tool", "tool_call_id": call_id, "name": name, "content": result_text})
                try:
                    normalised_arguments = self._normalise_args(function_data.get("arguments", {}))
                except WorkspaceError:
                    normalised_arguments = {}
                self.context.record_tool_result(
                    name,
                    normalised_arguments,
                    {"ok": False, "cancelled": True, "error": "工具调用因用户取消而未执行。"},
                    call_id,
                )
            print("\n已取消当前请求。已完成的工具结果已保留，未完成的调用不会被视为成功。")
            return None
        except ModelRequestError as exc:
            task_status = "failed"
            executed = self.tool_runtime.executed_since(audit_start)
            if not executed:
                context_valid = False
                # Policy/fallback decisions survive a failed request; activation does not.
                policy = self.tool_runtime.policy.snapshot()
                retained_runtime = dict(runtime_snapshot, policy=policy,
                                        provider=self.provider_session.snapshot())
                if self.store is not None and request_offset is not None:
                    self.store.rollback_context(request_offset, retained_runtime,
                                                self._audit_events[audit_event_start:])
                self.messages[:] = message_snapshot
                self.context.restore(context_snapshot)
                self.tool_runtime.restore(dict(runtime_snapshot, policy=policy))
                self.tool_runtime.end_task(self._runtime_task_id)
            print(f"模型请求失败: {exc}")
            return None
        finally:
            self.messages[0] = task_system_message
            self.store.end_task(task_status, context_valid=context_valid)
            self.usage_ledger.summary()

    def compact_now(self, keep_tokens: int | None = None, reason: str = MANUAL) -> CompactionResult:
        """Run an explicit compaction and report what it did.

        This is the same service the automatic trigger uses, so the state effect
        is identical; it does not start a task and does not touch task_number.
        """

        result = self.context.compact(
            self.messages, self.tool_runtime.schemas(self._runtime_task_id), self.compression_client, reason, keep_tokens=keep_tokens
        )
        if result.compacted:
            print(
                f"[压缩/{result.reason}] {result.before_tokens} -> {result.after_tokens} tokens；"
                f"退休 {result.retired_messages} 条旧消息（收起 {len(result.compressed_call_ids)} 个工具结果）；"
                f"方法={result.method}；保留窗口={result.keep_tokens}"
                + (f"；{result.warning}" if result.warning else "")
            )
        else:
            print(f"[压缩/{result.reason}] 未压缩：{result.warning}")
        return result
