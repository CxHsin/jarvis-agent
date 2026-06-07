from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from hashlib import sha1
from pathlib import Path

from app.plugins import DriftContext, DriftOutcome, DriftTask, PluginSpec

PLUGIN_ID = "pending_review_drift"
DEFAULT_LOG_PATH = "memory/pending-review.log"


def _read_positive_int(raw: object, default: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class _PendingReviewTaskPayload:
    pending_text: str
    log_path: Path
    preview_lines: int

    def execute(self, context: DriftContext) -> DriftOutcome:
        lines = [line.strip() for line in self.pending_text.splitlines() if line.strip()]
        preview = " | ".join(lines[: self.preview_lines]) if lines else "(empty)"
        timestamp = context.now.astimezone(UTC).isoformat()
        entry = f"{timestamp} pending_lines={len(lines)} preview={preview}\n"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(entry)
        return DriftOutcome(summary=f"Recorded pending review with {len(lines)} lines")


def build_plugin(config: dict[str, object]) -> PluginSpec:
    merged = {
        "enabled": False,
        "priority": 3,
        "estimated_cost": 1,
        "log_path": DEFAULT_LOG_PATH,
        "preview_lines": 3,
    }
    merged.update(config)

    def _collect_tasks(context: DriftContext) -> list[DriftTask]:
        if not bool(merged.get("enabled", False)):
            return []
        snapshot = context.memory_snapshot
        if snapshot is None:
            return []
        pending_text = snapshot.pending_text.strip()
        if not pending_text:
            return []

        digest = sha1(pending_text.encode("utf-8")).hexdigest()[:12]
        payload = _PendingReviewTaskPayload(
            pending_text=pending_text,
            log_path=Path(str(merged.get("log_path", DEFAULT_LOG_PATH)).strip() or DEFAULT_LOG_PATH),
            preview_lines=_read_positive_int(merged.get("preview_lines"), 3),
        )
        return [
            DriftTask(
                task_id=f"{PLUGIN_ID}:{digest}",
                plugin_id=PLUGIN_ID,
                kind="pending_review",
                summary="Review pending memory entries and record a lightweight audit note.",
                execute=payload.execute,
                priority=_read_positive_int(merged.get("priority"), 3),
                dedupe_key=f"{PLUGIN_ID}:{digest}",
                estimated_cost=_read_positive_int(merged.get("estimated_cost"), 1),
            )
        ]

    return PluginSpec(
        plugin_id=PLUGIN_ID,
        plugin_name="Pending Review Drift",
        enabled_by_default=False,
        collect_drift_tasks=_collect_tasks,
        config=merged,
    )
