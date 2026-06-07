from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.drift import DriftConfig, DriftExecutionLog, DriftRunner, DriftRuntimeState
from app.memory_store import MemoryStore
from app.plugins import DriftContext, DriftOutcome, DriftTask, PluginHost, PluginSpec
from app.proactive import ProactiveRuntimeState
from app.tools import ToolRegistry


def _runner(
    tmp_path: Path,
    *,
    activity_state: ProactiveRuntimeState | None = None,
    tasks: list[DriftTask] | None = None,
) -> tuple[DriftRunner, DriftExecutionLog]:
    memory_store = MemoryStore(root_dir=tmp_path / "memory")
    memory_store.ensure_initialized()
    registry = ToolRegistry()
    plugin = PluginSpec(
        plugin_id="drift_fixture",
        plugin_name="Drift Fixture",
        collect_drift_tasks=lambda context: list(tasks or []),
    )
    host = PluginHost(registry=registry, plugins_package="app.plugins")
    host._plugins = [plugin]  # type: ignore[attr-defined]
    execution_log = DriftExecutionLog(path=tmp_path / "memory" / "drift_execution_log.json")
    runner = DriftRunner(
        config=DriftConfig(
            enabled=True,
            execution_log_path=tmp_path / "memory" / "drift_execution_log.json",
            tick_interval_seconds=60,
            idle_grace_seconds_after_user_message=300,
            idle_grace_seconds_after_proactive_send=300,
            dedupe_window_seconds=600,
            max_task_runtime_seconds=30,
            max_task_cost=3,
        ),
        plugin_host=host,
        memory_store=memory_store,
        activity_state=activity_state
        or ProactiveRuntimeState(
            last_user_message_at=datetime.now(UTC) - timedelta(seconds=900),
            last_proactive_send_at=datetime.now(UTC) - timedelta(seconds=900),
        ),
        runtime_state=DriftRuntimeState(),
        execution_log=execution_log,
    )
    return runner, execution_log


def test_runner_executes_single_task(tmp_path: Path) -> None:
    executed: list[str] = []

    def execute(context: DriftContext) -> DriftOutcome:
        executed.append("task-1")
        return DriftOutcome(summary="ok")

    runner, _ = _runner(
        tmp_path,
        tasks=[
            DriftTask(
                task_id="task-1",
                plugin_id="drift_fixture",
                kind="memory_maintenance",
                summary="compact memory",
                priority=10,
                dedupe_key="compact-memory",
                execute=execute,
            )
        ],
    )

    decision = runner.run_tick()

    assert decision.action == "executed"
    assert decision.task is not None
    assert decision.task.task_id == "task-1"
    assert executed == ["task-1"]


def test_runner_skips_when_user_was_recently_active(tmp_path: Path) -> None:
    runner, _ = _runner(
        tmp_path,
        activity_state=ProactiveRuntimeState(
            last_user_message_at=datetime.now(UTC) - timedelta(seconds=10),
            last_proactive_send_at=datetime.now(UTC) - timedelta(seconds=900),
        ),
    )

    decision = runner.run_tick()

    assert decision.action == "skip"
    assert decision.reason == "user_recently_active"


def test_runner_skips_recent_duplicate_execution(tmp_path: Path) -> None:
    def execute(context: DriftContext) -> DriftOutcome:
        return DriftOutcome(summary="ok")

    task = DriftTask(
        task_id="task-1",
        plugin_id="drift_fixture",
        kind="memory_maintenance",
        summary="compact memory",
        priority=10,
        dedupe_key="compact-memory",
        execute=execute,
    )
    runner, execution_log = _runner(tmp_path, tasks=[task])
    execution_log.append(
        at=datetime.now(UTC),
        result="executed",
        reason="task_executed",
        task=task,
    )

    decision = runner.run_tick()

    assert decision.action == "skip"
    assert decision.reason == "task_not_ready"


def test_runner_skips_when_passive_turn_is_in_progress(tmp_path: Path) -> None:
    state = ProactiveRuntimeState(
        last_user_message_at=datetime.now(UTC) - timedelta(seconds=900),
        last_proactive_send_at=datetime.now(UTC) - timedelta(seconds=900),
        passive_turn_in_progress=True,
    )
    runner, _ = _runner(tmp_path, activity_state=state)

    decision = runner.run_tick()

    assert decision.action == "skip"
    assert decision.reason == "passive_turn_in_progress"
