from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.memory_store import MemoryStore
from app.plugins import PluginHost, ProactiveCandidate
from app.proactive import (
    ProactiveConfig,
    ProactiveDeliveryLog,
    ProactiveRuntimeState,
    ProactiveScheduler,
)
from app.tools import ToolRegistry


class StubTelegramBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    def send_proactive_message(self, *, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


def _scheduler(
    tmp_path: Path,
    *,
    runtime_state: ProactiveRuntimeState | None = None,
) -> tuple[ProactiveScheduler, StubTelegramBot, ProactiveDeliveryLog]:
    memory_store = MemoryStore(root_dir=tmp_path / "memory")
    memory_store.ensure_initialized()
    host = PluginHost(registry=ToolRegistry(), plugins_package="tests.fixtures.proactive_plugins")
    host.initialize()
    telegram = StubTelegramBot()
    delivery_log = ProactiveDeliveryLog(path=tmp_path / "memory" / "proactive_delivery_log.json")
    scheduler = ProactiveScheduler(
        config=ProactiveConfig(
            enabled=True,
            chat_id=7,
            delivery_log_path=tmp_path / "memory" / "proactive_delivery_log.json",
            tick_interval_seconds=60,
            cooldown_seconds=600,
            user_active_grace_seconds=300,
            candidate_limit=3,
            max_sends_per_tick=1,
        ),
        plugin_host=host,
        memory_store=memory_store,
        telegram_bot=telegram,  # type: ignore[arg-type]
        runtime_state=runtime_state or ProactiveRuntimeState(),
        delivery_log=delivery_log,
    )
    return scheduler, telegram, delivery_log


def test_scheduler_sends_top_candidate(tmp_path: Path) -> None:
    scheduler, telegram, _ = _scheduler(tmp_path)

    decision = scheduler.run_tick()

    assert decision.action == "send"
    assert telegram.messages == [(7, "Remember to drink water.")]
    assert scheduler.runtime_state.last_proactive_send_at is not None


def test_scheduler_skips_when_user_was_recently_active(tmp_path: Path) -> None:
    state = ProactiveRuntimeState(
        last_user_message_at=datetime.now(UTC) - timedelta(seconds=30),
    )
    scheduler, telegram, _ = _scheduler(tmp_path, runtime_state=state)

    decision = scheduler.run_tick()

    assert decision.action == "skip"
    assert decision.reason == "user_recently_active"
    assert telegram.messages == []


def test_scheduler_skips_recent_duplicate_send(tmp_path: Path) -> None:
    scheduler, telegram, delivery_log = _scheduler(tmp_path)
    now = datetime.now(UTC)
    delivery_log.append(
        at=now,
        chat_id=7,
        result="sent",
        reason="candidate_selected",
        candidate=ProactiveCandidate(
            candidate_id="cand-1",
            plugin_id="simple_candidate",
            kind="reminder",
            summary="Remember to drink water.",
            priority=10,
            dedupe_key="water-reminder",
            suggested_message="Remember to drink water.",
        ),
    )

    decision = scheduler.run_tick()

    assert decision.action == "skip"
    assert decision.reason == "duplicate_recent_send"
    assert telegram.messages == []


def test_scheduler_honors_cooldown(tmp_path: Path) -> None:
    state = ProactiveRuntimeState(
        last_proactive_send_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    scheduler, telegram, _ = _scheduler(tmp_path, runtime_state=state)

    decision = scheduler.run_tick()

    assert decision.action == "skip"
    assert decision.reason == "cooldown_active"
    assert telegram.messages == []
