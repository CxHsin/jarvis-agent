from datetime import UTC, datetime

from app.memory_store import ConsolidationState, MemorySnapshot
from app.plugins import DriftContext, PluginHost
from app.tools import ToolRegistry


def test_pending_review_drift_plugin_emits_task_and_writes_log(tmp_path) -> None:
    log_path = tmp_path / "pending-review.log"
    host = PluginHost(
        registry=ToolRegistry(),
        enabled_plugins=("pending_review_drift",),
        plugin_configs={
            "pending_review_drift": {
                "enabled": True,
                "log_path": str(log_path),
                "preview_lines": 2,
                "priority": 4,
            }
        },
    )
    host.initialize()
    context = DriftContext(
        now=datetime.now(UTC),
        last_user_message_at=None,
        last_proactive_send_at=None,
        memory_snapshot=MemorySnapshot(
            self_text="",
            memory_text="",
            recent_context_text="",
            pending_text="follow up on taxes\nask about travel dates\n",
            history_text="",
            consolidation_state=ConsolidationState(),
        ),
        available_tools=(),
        enabled_plugin_ids=host.loaded_plugin_ids,
    )

    tasks = host.collect_drift_tasks(context)

    assert len(tasks) == 1
    assert tasks[0].plugin_id == "pending_review_drift"
    assert tasks[0].priority == 4

    outcome = tasks[0].execute(context)

    assert "Recorded pending review" in outcome.summary
    content = log_path.read_text(encoding="utf-8")
    assert "pending_lines=2" in content
    assert "follow up on taxes | ask about travel dates" in content


def test_pending_review_drift_plugin_skips_when_pending_is_empty() -> None:
    host = PluginHost(
        registry=ToolRegistry(),
        enabled_plugins=("pending_review_drift",),
        plugin_configs={"pending_review_drift": {"enabled": True}},
    )
    host.initialize()

    tasks = host.collect_drift_tasks(
        DriftContext(
            now=datetime.now(UTC),
            last_user_message_at=None,
            last_proactive_send_at=None,
            memory_snapshot=MemorySnapshot(
                self_text="",
                memory_text="",
                recent_context_text="",
                pending_text="   \n",
                history_text="",
                consolidation_state=ConsolidationState(),
            ),
            available_tools=(),
            enabled_plugin_ids=host.loaded_plugin_ids,
        )
    )

    assert tasks == []
