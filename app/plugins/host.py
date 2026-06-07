from __future__ import annotations

import importlib
import logging
import pkgutil
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, replace

from app.memory_normalizer import MemoryEntry
from app.tools.base import ToolSpec
from app.tools.registry import DuplicateToolError, ToolRegistry

from app.plugins.types import (
    DriftContext,
    DriftTask,
    MemoryWriteContext,
    ModelCallContext,
    ModelCallResult,
    ProactiveCandidate,
    ProactiveContext,
    PluginOutcome,
    PluginSpec,
    TurnContext,
    TurnNote,
    TurnResult,
)

logger = logging.getLogger(__name__)
BuildPluginFn = Callable[[dict[str, object]], PluginSpec]


class PluginError(RuntimeError):
    """Raised when plugin loading or validation fails."""


@dataclass(frozen=True)
class PluginLoadFailure:
    plugin_id: str | None
    module_name: str
    stage: str
    message: str


class PluginHost:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        enabled_plugins: tuple[str, ...] = (),
        disabled_plugins: tuple[str, ...] = (),
        plugin_configs: dict[str, dict[str, object]] | None = None,
        plugins_package: str = "app.plugins",
    ) -> None:
        self._registry = registry
        self._enabled = set(enabled_plugins)
        self._disabled = set(disabled_plugins)
        self._plugin_configs = plugin_configs or {}
        self._plugins_package = plugins_package
        self._plugins: list[PluginSpec] = []
        self._loaded_ids: list[str] = []
        self._disabled_ids: list[str] = []
        self._failures: list[PluginLoadFailure] = []

    @property
    def loaded_plugin_ids(self) -> tuple[str, ...]:
        return tuple(self._loaded_ids)

    @property
    def disabled_plugin_ids(self) -> tuple[str, ...]:
        return tuple(self._disabled_ids)

    @property
    def load_failures(self) -> tuple[PluginLoadFailure, ...]:
        return tuple(self._failures)

    @property
    def available_tools(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self._registry.list_specs())

    @property
    def proactive_plugin_ids(self) -> tuple[str, ...]:
        return tuple(
            plugin.plugin_id for plugin in self._plugins if plugin.collect_proactive_candidates is not None
        )

    def initialize(self) -> None:
        discovered = self._discover_plugins()
        enabled_specs = self._filter_enabled_plugins(discovered)
        self._plugins = self._order_plugins(enabled_specs)
        self._loaded_ids = [plugin.plugin_id for plugin in self._plugins]
        self._register_plugin_tools()
        logger.info("Loaded plugins: %s", ", ".join(self._loaded_ids) or "(none)")
        if self._disabled_ids:
            logger.info("Disabled plugins: %s", ", ".join(self._disabled_ids))
        for failure in self._failures:
            logger.warning(
                "Plugin load failed: module=%s plugin_id=%s stage=%s message=%s",
                failure.module_name,
                failure.plugin_id or "<unknown>",
                failure.stage,
                failure.message,
            )

    def build_context(self, context: TurnContext) -> list[str]:
        return self._run_hook_list(
            hook_name="build_context",
            invoke=lambda plugin: plugin.build_context(context) if plugin.build_context else None,
            validator=_validate_text_list,
            default=[],
            chat_id=context.chat_id,
        )

    def before_model_call(self, context: ModelCallContext) -> list[str]:
        return self._run_hook_list(
            hook_name="before_model_call",
            invoke=(
                lambda plugin: plugin.before_model_call(context) if plugin.before_model_call else None
            ),
            validator=_validate_text_list,
            default=[],
            chat_id=context.chat_id,
        )

    def after_model_call(self, context: ModelCallResult) -> list[TurnNote]:
        return self._run_hook_list(
            hook_name="after_model_call",
            invoke=lambda plugin: plugin.after_model_call(context) if plugin.after_model_call else None,
            validator=_validate_turn_notes,
            default=[],
            chat_id=context.chat_id,
        )

    def before_memory_write(self, context: MemoryWriteContext) -> list[MemoryEntry]:
        return self._run_hook_list(
            hook_name="before_memory_write",
            invoke=(
                lambda plugin: plugin.before_memory_write(context)
                if plugin.before_memory_write
                else None
            ),
            validator=_validate_memory_entries,
            default=[],
            chat_id=context.chat_id,
        )

    def after_turn(self, result: TurnResult) -> list[PluginOutcome]:
        return self._run_hook_list(
            hook_name="after_turn",
            invoke=lambda plugin: plugin.after_turn(result) if plugin.after_turn else None,
            validator=_validate_outcomes,
            default=[],
            chat_id=result.chat_id,
        )

    def collect_proactive_candidates(self, context: ProactiveContext) -> list[ProactiveCandidate]:
        return self._run_hook_list(
            hook_name="collect_proactive_candidates",
            invoke=(
                lambda plugin: plugin.collect_proactive_candidates(context)
                if plugin.collect_proactive_candidates
                else None
            ),
            validator=_validate_proactive_candidates,
            default=[],
            chat_id=context.chat_id,
        )

    def collect_drift_tasks(self, context: DriftContext) -> list[DriftTask]:
        return self._run_hook_list(
            hook_name="collect_drift_tasks",
            invoke=(
                lambda plugin: plugin.collect_drift_tasks(context)
                if plugin.collect_drift_tasks
                else None
            ),
            validator=_validate_drift_tasks,
            default=[],
            chat_id=0,
        )

    def _discover_plugins(self) -> list[PluginSpec]:
        try:
            package = importlib.import_module(self._plugins_package)
        except Exception as exc:
            raise PluginError(f"Failed to import plugin package {self._plugins_package}") from exc

        package_path = getattr(package, "__path__", None)
        if package_path is None:
            raise PluginError(f"Plugin package {self._plugins_package} is not a package")

        plugins: list[PluginSpec] = []
        seen_ids: set[str] = set()
        for module_info in pkgutil.iter_modules(package_path):
            if not module_info.name.endswith("_plugin"):
                continue
            module_name = f"{self._plugins_package}.{module_info.name}"
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:
                self._failures.append(
                    PluginLoadFailure(
                        plugin_id=None,
                        module_name=module_name,
                        stage="import",
                        message=str(exc),
                    )
                )
                continue

            plugin = getattr(module, "PLUGIN", None)
            builder = getattr(module, "build_plugin", None)
            plugin_id_hint = getattr(module, "PLUGIN_ID", None)
            if builder is not None:
                try:
                    plugin = self._build_plugin_from_factory(
                        module_name=module_name,
                        builder=builder,
                        plugin_id_hint=plugin_id_hint,
                    )
                except PluginError as exc:
                    self._failures.append(
                        PluginLoadFailure(
                            plugin_id=plugin_id_hint,
                            module_name=module_name,
                            stage="validation",
                            message=str(exc),
                        )
                    )
                    continue
            elif not isinstance(plugin, PluginSpec):
                self._failures.append(
                    PluginLoadFailure(
                        plugin_id=None,
                        module_name=module_name,
                        stage="validation",
                        message="Module must export PLUGIN or build_plugin(config).",
                    )
                )
                continue

            if plugin.plugin_id in seen_ids:
                self._failures.append(
                    PluginLoadFailure(
                        plugin_id=plugin.plugin_id,
                        module_name=module_name,
                        stage="validation",
                        message="Duplicate plugin ID.",
                    )
                )
                continue
            seen_ids.add(plugin.plugin_id)
            plugins.append(plugin if builder is not None else self._apply_plugin_config(plugin))
        return plugins

    def _filter_enabled_plugins(self, plugins: list[PluginSpec]) -> list[PluginSpec]:
        enabled: list[PluginSpec] = []
        self._disabled_ids = []
        for plugin in plugins:
            is_enabled = (plugin.enabled_by_default or plugin.plugin_id in self._enabled) and (
                plugin.plugin_id not in self._disabled
            )
            if is_enabled:
                enabled.append(plugin)
            else:
                self._disabled_ids.append(plugin.plugin_id)
        return enabled

    def _order_plugins(self, plugins: list[PluginSpec]) -> list[PluginSpec]:
        by_id = {plugin.plugin_id: plugin for plugin in plugins}
        graph: dict[str, set[str]] = defaultdict(set)
        indegree = {plugin.plugin_id: 0 for plugin in plugins}

        for plugin in plugins:
            for before_id in plugin.before:
                if before_id not in by_id:
                    continue
                if before_id not in graph[plugin.plugin_id]:
                    graph[plugin.plugin_id].add(before_id)
                    indegree[before_id] += 1
            for after_id in plugin.after:
                if after_id not in by_id:
                    continue
                if plugin.plugin_id not in graph[after_id]:
                    graph[after_id].add(plugin.plugin_id)
                    indegree[plugin.plugin_id] += 1

        queue = deque(sorted((plugin_id for plugin_id, value in indegree.items() if value == 0)))
        ordered_ids: list[str] = []
        while queue:
            plugin_id = queue.popleft()
            ordered_ids.append(plugin_id)
            for neighbor in sorted(graph[plugin_id]):
                indegree[neighbor] -= 1
                if indegree[neighbor] == 0:
                    queue.append(neighbor)

        if len(ordered_ids) != len(plugins):
            cycle_ids = sorted(plugin_id for plugin_id, value in indegree.items() if value > 0)
            for plugin_id in cycle_ids:
                self._failures.append(
                    PluginLoadFailure(
                        plugin_id=plugin_id,
                        module_name=self._plugins_package,
                        stage="ordering",
                        message="Conflicting before/after directives.",
                    )
                )
            return [by_id[plugin_id] for plugin_id in sorted(by_id) if plugin_id not in cycle_ids]

        return [by_id[plugin_id] for plugin_id in ordered_ids]

    def _register_plugin_tools(self) -> None:
        for plugin in self._plugins:
            if plugin.register_tools is None:
                continue
            try:
                tools = plugin.register_tools()
                validated = _validate_tool_specs(tools)
                for tool in validated:
                    self._registry.register(tool)
            except (PluginError, DuplicateToolError) as exc:
                self._failures.append(
                    PluginLoadFailure(
                        plugin_id=plugin.plugin_id,
                        module_name=self._plugins_package,
                        stage="register_tools",
                        message=str(exc),
                    )
                )
                logger.exception("Plugin tool registration failed for %s", plugin.plugin_id)

    def _run_hook_list(
        self,
        *,
        hook_name: str,
        invoke,
        validator,
        default,
        chat_id: int,
    ):
        results = list(default)
        for plugin in self._plugins:
            try:
                payload = invoke(plugin)
            except Exception:
                logger.exception(
                    "Plugin hook failed: plugin_id=%s hook_name=%s chat_id=%s",
                    plugin.plugin_id,
                    hook_name,
                    chat_id,
                )
                continue
            if payload is None:
                continue
            try:
                validated = validator(payload)
            except PluginError as exc:
                logger.warning(
                    "Plugin contribution rejected: plugin_id=%s hook_name=%s chat_id=%s message=%s",
                    plugin.plugin_id,
                    hook_name,
                    chat_id,
                    exc,
                )
                continue
            results.extend(validated)
        return results

    def _apply_plugin_config(self, plugin: PluginSpec) -> PluginSpec:
        overrides = self._plugin_configs.get(plugin.plugin_id)
        if not overrides:
            return plugin
        merged = dict(plugin.config)
        merged.update(overrides)
        return replace(plugin, config=merged)

    def _build_plugin_from_factory(
        self,
        *,
        module_name: str,
        builder: object,
        plugin_id_hint: str | None,
    ) -> PluginSpec:
        if not callable(builder):
            raise PluginError(f"{module_name}.build_plugin must be callable.")
        overrides = dict(self._plugin_configs.get(plugin_id_hint or "", {}))
        plugin = builder(overrides)
        if not isinstance(plugin, PluginSpec):
            raise PluginError(f"{module_name}.build_plugin must return PluginSpec.")
        return plugin


def _validate_text_list(payload: object) -> list[str]:
    if not isinstance(payload, list):
        raise PluginError("Expected a list of strings.")
    values: list[str] = []
    for item in payload:
        if not isinstance(item, str) or not item.strip():
            raise PluginError("Expected a list of non-empty strings.")
        values.append(item.strip())
    return values


def _validate_turn_notes(payload: object) -> list[TurnNote]:
    if not isinstance(payload, list) or not all(isinstance(item, TurnNote) for item in payload):
        raise PluginError("Expected a list of TurnNote.")
    return list(payload)


def _validate_memory_entries(payload: object) -> list[MemoryEntry]:
    if not isinstance(payload, list) or not all(isinstance(item, MemoryEntry) for item in payload):
        raise PluginError("Expected a list of MemoryEntry.")
    return list(payload)


def _validate_outcomes(payload: object) -> list[PluginOutcome]:
    if payload is None:
        return []
    if isinstance(payload, PluginOutcome):
        return [payload]
    raise PluginError("Expected PluginOutcome or None.")


def _validate_tool_specs(payload: object) -> list[ToolSpec]:
    if not isinstance(payload, list) or not all(isinstance(item, ToolSpec) for item in payload):
        raise PluginError("Expected a list of ToolSpec.")
    return list(payload)


def _validate_proactive_candidates(payload: object) -> list[ProactiveCandidate]:
    if not isinstance(payload, list) or not all(
        isinstance(item, ProactiveCandidate) for item in payload
    ):
        raise PluginError("Expected a list of ProactiveCandidate.")
    candidates: list[ProactiveCandidate] = []
    for item in payload:
        if not item.candidate_id.strip():
            raise PluginError("ProactiveCandidate.candidate_id must be non-empty.")
        if not item.plugin_id.strip():
            raise PluginError("ProactiveCandidate.plugin_id must be non-empty.")
        if not item.kind.strip():
            raise PluginError("ProactiveCandidate.kind must be non-empty.")
        if not item.summary.strip():
            raise PluginError("ProactiveCandidate.summary must be non-empty.")
        candidates.append(item)
    return candidates


def _validate_drift_tasks(payload: object) -> list[DriftTask]:
    if not isinstance(payload, list) or not all(isinstance(item, DriftTask) for item in payload):
        raise PluginError("Expected a list of DriftTask.")
    tasks: list[DriftTask] = []
    for item in payload:
        if not item.task_id.strip():
            raise PluginError("DriftTask.task_id must be non-empty.")
        if not item.plugin_id.strip():
            raise PluginError("DriftTask.plugin_id must be non-empty.")
        if not item.kind.strip():
            raise PluginError("DriftTask.kind must be non-empty.")
        if not item.summary.strip():
            raise PluginError("DriftTask.summary must be non-empty.")
        if item.estimated_cost <= 0:
            raise PluginError("DriftTask.estimated_cost must be greater than zero.")
        tasks.append(item)
    return tasks
