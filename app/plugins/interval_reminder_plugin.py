from __future__ import annotations

from datetime import UTC, timedelta

from app.plugins import PluginSpec, ProactiveCandidate, ProactiveContext

PLUGIN_ID = "interval_reminder"
DEFAULT_INTERVAL_MINUTES = 240


def _read_positive_int(raw: object, default: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def build_plugin(config: dict[str, object]) -> PluginSpec:
    merged = {
        "enabled": False,
        "interval_minutes": DEFAULT_INTERVAL_MINUTES,
        "priority": 5,
        "message": "",
    }
    merged.update(config)

    def _collect_candidates(context: ProactiveContext) -> list[ProactiveCandidate]:
        enabled = bool(merged.get("enabled", False))
        if not enabled:
            return []

        message = str(merged.get("message", "")).strip()
        if not message:
            return []

        interval_minutes = _read_positive_int(
            merged.get("interval_minutes"),
            DEFAULT_INTERVAL_MINUTES,
        )
        now = context.now.astimezone(UTC)
        last_send = (
            context.last_proactive_send_at.astimezone(UTC)
            if context.last_proactive_send_at is not None
            else None
        )
        if last_send is not None and now < last_send + timedelta(minutes=interval_minutes):
            return []

        return [
            ProactiveCandidate(
                candidate_id=f"{PLUGIN_ID}:{now.strftime('%Y%m%d%H')}",
                plugin_id=PLUGIN_ID,
                kind="interval_reminder",
                summary=message,
                priority=_read_positive_int(merged.get("priority"), 5),
                dedupe_key=f"{PLUGIN_ID}:{message}",
                suggested_message=message,
            )
        ]

    return PluginSpec(
        plugin_id=PLUGIN_ID,
        plugin_name="Interval Reminder",
        enabled_by_default=False,
        collect_proactive_candidates=_collect_candidates,
        config=merged,
    )
