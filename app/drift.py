from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.memory_store import MemorySnapshot, MemoryStore, MemoryStoreError
from app.plugins import DriftContext, DriftOutcome, DriftTask, PluginHost
from app.proactive import ProactiveRuntimeState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DriftConfig:
    enabled: bool
    execution_log_path: Path
    tick_interval_seconds: int
    idle_grace_seconds_after_user_message: int
    idle_grace_seconds_after_proactive_send: int
    dedupe_window_seconds: int
    max_task_runtime_seconds: int
    max_task_cost: int


@dataclass(frozen=True)
class DriftDecision:
    action: str
    reason: str
    task: DriftTask | None = None
    outcome: DriftOutcome | None = None


@dataclass
class DriftRuntimeState:
    last_tick_at: datetime | None = None
    last_task_started_at: datetime | None = None
    last_task_finished_at: datetime | None = None
    last_successful_task_at: datetime | None = None
    currently_running_task_id: str | None = None
    consecutive_skip_count: int = 0


class DriftExecutionLog:
    def __init__(self, *, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def append(
        self,
        *,
        at: datetime,
        result: str,
        reason: str,
        task: DriftTask | None,
        duration_seconds: float | None = None,
    ) -> None:
        entry = {
            "timestamp": at.isoformat(),
            "result": result,
            "reason": reason,
            "task_id": task.task_id if task else None,
            "plugin_id": task.plugin_id if task else None,
            "kind": task.kind if task else None,
            "dedupe_key": task.dedupe_key if task else None,
            "duration_seconds": duration_seconds,
        }
        with self._lock:
            existing = []
            if self._path.exists():
                try:
                    raw = self._path.read_text(encoding="utf-8").strip()
                except OSError as exc:
                    raise MemoryStoreError(
                        f"Failed to read drift execution log: {self._path}"
                    ) from exc
                if raw:
                    try:
                        existing = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise MemoryStoreError(
                            f"Failed to parse drift execution log: {self._path}"
                        ) from exc
                    if not isinstance(existing, list):
                        raise MemoryStoreError(
                            f"Failed to parse drift execution log: {self._path}"
                        )
            existing.append(entry)
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text(
                    json.dumps(existing, indent=2, ensure_ascii=True) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                raise MemoryStoreError(
                    f"Failed to write drift execution log: {self._path}"
                ) from exc

    def was_recently_executed(
        self,
        *,
        dedupe_key: str,
        now: datetime,
        within_seconds: int,
    ) -> bool:
        if not dedupe_key.strip() or not self._path.exists():
            return False
        try:
            raw = self._path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise MemoryStoreError(f"Failed to read drift execution log: {self._path}") from exc
        if not raw:
            return False
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MemoryStoreError(
                f"Failed to parse drift execution log: {self._path}"
            ) from exc
        if not isinstance(entries, list):
            raise MemoryStoreError(f"Failed to parse drift execution log: {self._path}")
        cutoff = now - timedelta(seconds=within_seconds)
        for item in reversed(entries):
            if not isinstance(item, dict):
                continue
            if item.get("result") != "executed":
                continue
            if item.get("dedupe_key") != dedupe_key:
                continue
            timestamp = item.get("timestamp")
            if not isinstance(timestamp, str):
                continue
            try:
                executed_at = datetime.fromisoformat(timestamp)
            except ValueError:
                continue
            if executed_at >= cutoff:
                return True
        return False


class DriftTaskCollector:
    def __init__(self, *, plugin_host: PluginHost) -> None:
        self._plugin_host = plugin_host

    def collect(self, *, context: DriftContext) -> list[DriftTask]:
        return self._plugin_host.collect_drift_tasks(context)


class DriftTaskFilter:
    def __init__(
        self,
        *,
        config: DriftConfig,
        activity_state: ProactiveRuntimeState,
        runtime_state: DriftRuntimeState,
        execution_log: DriftExecutionLog,
    ) -> None:
        self._config = config
        self._activity_state = activity_state
        self._runtime_state = runtime_state
        self._execution_log = execution_log

    def apply(
        self,
        *,
        tasks: list[DriftTask],
        now: datetime,
    ) -> tuple[list[DriftTask], str]:
        if self._activity_state.passive_turn_in_progress:
            return [], "passive_turn_in_progress"
        if self._runtime_state.currently_running_task_id is not None:
            return [], "drift_already_running"
        if self._activity_state.last_user_message_at is None and self._activity_state.last_proactive_send_at is None:
            return [], "runtime_state_unavailable"
        if self._activity_state.last_user_message_at is not None:
            user_ready_at = self._activity_state.last_user_message_at + timedelta(
                seconds=self._config.idle_grace_seconds_after_user_message
            )
            if now < user_ready_at:
                return [], "user_recently_active"
        if self._activity_state.last_proactive_send_at is not None:
            proactive_ready_at = self._activity_state.last_proactive_send_at + timedelta(
                seconds=self._config.idle_grace_seconds_after_proactive_send
            )
            if now < proactive_ready_at:
                return [], "recent_proactive_send"
        if not tasks:
            return [], "no_tasks"
        accepted: list[DriftTask] = []
        seen_keys: set[str] = set()
        for task in tasks:
            if task.not_before is not None and task.not_before > now:
                continue
            dedupe_key = (task.dedupe_key or task.task_id).strip()
            if dedupe_key in seen_keys:
                continue
            if task.estimated_cost > self._config.max_task_cost:
                continue
            if self._execution_log.was_recently_executed(
                dedupe_key=dedupe_key,
                now=now,
                within_seconds=self._config.dedupe_window_seconds,
            ):
                continue
            seen_keys.add(dedupe_key)
            accepted.append(task)
        if not accepted:
            return [], "task_not_ready"
        accepted.sort(
            key=lambda item: (
                -item.priority,
                item.not_before or datetime.min.replace(tzinfo=UTC),
                item.task_id,
            )
        )
        return accepted[:1], "task_selected"


class DriftRunner:
    def __init__(
        self,
        *,
        config: DriftConfig,
        plugin_host: PluginHost,
        memory_store: MemoryStore,
        activity_state: ProactiveRuntimeState,
        runtime_state: DriftRuntimeState | None = None,
        execution_log: DriftExecutionLog | None = None,
    ) -> None:
        self._config = config
        self._plugin_host = plugin_host
        self._memory_store = memory_store
        self._activity_state = activity_state
        self._runtime_state = runtime_state or DriftRuntimeState()
        self._execution_log = execution_log or DriftExecutionLog(
            path=self._config.execution_log_path
        )
        self._collector = DriftTaskCollector(plugin_host=self._plugin_host)
        self._filter = DriftTaskFilter(
            config=self._config,
            activity_state=self._activity_state,
            runtime_state=self._runtime_state,
            execution_log=self._execution_log,
        )
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def runtime_state(self) -> DriftRuntimeState:
        return self._runtime_state

    def start(self) -> None:
        if not self._config.enabled or self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self.run_forever, name="drift-runner", daemon=True)
        self._thread.start()
        logger.info("Started drift runner")

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1)
        self._thread = None
        logger.info("Stopped drift runner")

    def run_forever(self) -> None:
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                self.run_tick()
            except Exception:
                logger.exception("Drift tick failed")
            elapsed = time.monotonic() - started
            remaining = max(0.0, self._config.tick_interval_seconds - elapsed)
            self._stop_event.wait(remaining)

    def run_tick(self) -> DriftDecision:
        now = datetime.now(UTC)
        self._runtime_state.last_tick_at = now
        snapshot = self._load_memory_snapshot()
        context = DriftContext(
            now=now,
            last_user_message_at=self._activity_state.last_user_message_at,
            last_proactive_send_at=self._activity_state.last_proactive_send_at,
            memory_snapshot=snapshot,
            available_tools=self._plugin_host.available_tools,
            enabled_plugin_ids=self._plugin_host.loaded_plugin_ids,
        )
        tasks = self._collector.collect(context=context)
        logger.info("Collected drift tasks", extra={"task_count": len(tasks)})
        filtered, reason = self._filter.apply(tasks=tasks, now=now)
        if not filtered:
            decision = DriftDecision(action="skip", reason=reason)
            self._record_outcome(at=now, decision=decision, result="skipped")
            self._runtime_state.consecutive_skip_count += 1
            return decision
        task = filtered[0]
        self._runtime_state.currently_running_task_id = task.task_id
        self._runtime_state.last_task_started_at = now
        started = time.monotonic()
        try:
            outcome = task.execute(context)
        except Exception:
            logger.exception("Drift task execution failed", extra={"task_id": task.task_id})
            self._runtime_state.currently_running_task_id = None
            self._runtime_state.last_task_finished_at = datetime.now(UTC)
            decision = DriftDecision(action="failed", reason="task_failed", task=task)
            self._record_outcome(
                at=now,
                decision=decision,
                result="failed",
                duration_seconds=time.monotonic() - started,
            )
            return decision
        finished_at = datetime.now(UTC)
        self._runtime_state.currently_running_task_id = None
        self._runtime_state.last_task_finished_at = finished_at
        self._runtime_state.last_successful_task_at = finished_at
        self._runtime_state.consecutive_skip_count = 0
        decision = DriftDecision(
            action="executed",
            reason=outcome.reason,
            task=task,
            outcome=outcome,
        )
        self._record_outcome(
            at=now,
            decision=decision,
            result="executed",
            duration_seconds=time.monotonic() - started,
        )
        return decision

    def _load_memory_snapshot(self) -> MemorySnapshot | None:
        try:
            return self._memory_store.load_snapshot()
        except MemoryStoreError:
            logger.exception("Failed to load memory for drift runner")
            return None

    def _record_outcome(
        self,
        *,
        at: datetime,
        decision: DriftDecision,
        result: str,
        duration_seconds: float | None = None,
    ) -> None:
        try:
            self._execution_log.append(
                at=at,
                result=result,
                reason=decision.reason,
                task=decision.task,
                duration_seconds=duration_seconds,
            )
        except MemoryStoreError:
            logger.exception("Failed to persist drift outcome")
