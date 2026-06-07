from datetime import UTC, datetime, timedelta

from app.plugins import PluginHost, ProactiveContext
from app.tools import ToolRegistry


def test_interval_reminder_plugin_emits_candidate_when_enabled() -> None:
    host = PluginHost(
        registry=ToolRegistry(),
        enabled_plugins=("interval_reminder",),
        plugin_configs={
            "interval_reminder": {
                "enabled": True,
                "interval_minutes": 240,
                "priority": 7,
                "message": "Time to take a break and drink water.",
            }
        },
    )
    host.initialize()

    candidates = host.collect_proactive_candidates(
        ProactiveContext(
            chat_id=7,
            now=datetime.now(UTC),
            last_user_message_at=None,
            last_proactive_send_at=None,
            memory_snapshot=None,
            available_tools=(),
            enabled_plugin_ids=host.loaded_plugin_ids,
        )
    )

    assert len(candidates) == 1
    assert candidates[0].plugin_id == "interval_reminder"
    assert candidates[0].suggested_message == "Time to take a break and drink water."
    assert candidates[0].priority == 7


def test_interval_reminder_plugin_respects_interval() -> None:
    host = PluginHost(
        registry=ToolRegistry(),
        enabled_plugins=("interval_reminder",),
        plugin_configs={
            "interval_reminder": {
                "enabled": True,
                "interval_minutes": 240,
                "message": "Time to take a break and drink water.",
            }
        },
    )
    host.initialize()

    candidates = host.collect_proactive_candidates(
        ProactiveContext(
            chat_id=7,
            now=datetime.now(UTC),
            last_user_message_at=None,
            last_proactive_send_at=datetime.now(UTC) - timedelta(minutes=30),
            memory_snapshot=None,
            available_tools=(),
            enabled_plugin_ids=host.loaded_plugin_ids,
        )
    )

    assert candidates == []
