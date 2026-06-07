from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.memory_store import MemorySnapshot, MemoryStore, MemoryStoreError
from app.plugins import PluginHost, ProactiveCandidate, ProactiveContext
from app.telegram_bot import TelegramBot, TelegramAPIError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProactiveConfig:
    enabled: bool
    chat_id: int
    delivery_log_path: Path
    tick_interval_seconds: int
    cooldown_seconds: int
    user_active_grace_seconds: int
    candidate_limit: int
    max_sends_per_tick: int


@dataclass(frozen=True)
class ProactiveDecision:
    action: str
    reason: str
    candidate: ProactiveCandidate | None = None
    message_text: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass
class ProactiveRuntimeState:
    last_tick_at: datetime | None = None
    last_user_message_at: datetime | None = None
    last_proactive_send_at: datetime | None = None
    consecutive_empty_ticks: int = 0

    def record_user_message(self, when: datetime | None = None) -> None:
        self.last_user_message_at = when or datetime.now(UTC)


class ProactiveDeliveryLog:
    def __init__(self, *, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def append(
        self,
        *,
        at: datetime,
        chat_id: int,
        result: str,
        reason: str,
        candidate: ProactiveCandidate | None,
    ) -> None:
        entry = {
            "timestamp": at.isoformat(),
            "chat_id": chat_id,
            "result": result,
            "reason": reason,
            "candidate_id": candidate.candidate_id if candidate else None,
            "plugin_id": candidate.plugin_id if candidate else None,
            "dedupe_key": candidate.dedupe_key if candidate else None,
        }
        with self._lock:
            existing = []
            if self._path.exists():
                try:
                    raw = self._path.read_text(encoding="utf-8").strip()
                except OSError as exc:
                    raise MemoryStoreError(
                        f"Failed to read proactive delivery log: {self._path}"
                    ) from exc
                if raw:
                    try:
                        existing = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise MemoryStoreError(
                            f"Failed to parse proactive delivery log: {self._path}"
                        ) from exc
                    if not isinstance(existing, list):
                        raise MemoryStoreError(
                            f"Failed to parse proactive delivery log: {self._path}"
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
                    f"Failed to write proactive delivery log: {self._path}"
                ) from exc

    def was_recently_sent(
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
            raise MemoryStoreError(f"Failed to read proactive delivery log: {self._path}") from exc
        if not raw:
            return False
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MemoryStoreError(
                f"Failed to parse proactive delivery log: {self._path}"
            ) from exc
        if not isinstance(entries, list):
            raise MemoryStoreError(f"Failed to parse proactive delivery log: {self._path}")
        cutoff = now - timedelta(seconds=within_seconds)
        for item in reversed(entries):
            if not isinstance(item, dict):
                continue
            if item.get("result") != "sent":
                continue
            if item.get("dedupe_key") != dedupe_key:
                continue
            timestamp = item.get("timestamp")
            if not isinstance(timestamp, str):
                continue
            try:
                sent_at = datetime.fromisoformat(timestamp)
            except ValueError:
                continue
            if sent_at >= cutoff:
                return True
        return False


class ProactiveJudge:
    def decide(self, *, candidates: list[ProactiveCandidate]) -> ProactiveDecision:
        if not candidates:
            return ProactiveDecision(action="skip", reason="no_candidates")
        top = candidates[0]
        message_text = (top.suggested_message or top.summary).strip()
        if not message_text:
            return ProactiveDecision(
                action="skip",
                reason="candidate_rejected",
                candidate=top,
            )
        return ProactiveDecision(
            action="send",
            reason="candidate_selected",
            candidate=top,
            message_text=message_text,
            evidence=top.evidence,
        )


class ProactiveCandidateCollector:
    def __init__(self, *, plugin_host: PluginHost) -> None:
        self._plugin_host = plugin_host

    def collect(self, *, context: ProactiveContext) -> list[ProactiveCandidate]:
        return self._plugin_host.collect_proactive_candidates(context)


class ProactiveCandidateFilter:
    def __init__(
        self,
        *,
        config: ProactiveConfig,
        runtime_state: ProactiveRuntimeState,
        delivery_log: ProactiveDeliveryLog,
    ) -> None:
        self._config = config
        self._runtime_state = runtime_state
        self._delivery_log = delivery_log

    def apply(
        self,
        *,
        candidates: list[ProactiveCandidate],
        now: datetime,
    ) -> tuple[list[ProactiveCandidate], str]:
        if not candidates:
            return [], "no_candidates"
        if self._runtime_state.last_proactive_send_at is not None:
            next_allowed = self._runtime_state.last_proactive_send_at + timedelta(
                seconds=self._config.cooldown_seconds
            )
            if now < next_allowed:
                return [], "cooldown_active"
        if self._runtime_state.last_user_message_at is not None:
            user_ready_at = self._runtime_state.last_user_message_at + timedelta(
                seconds=self._config.user_active_grace_seconds
            )
            if now < user_ready_at:
                return [], "user_recently_active"
        accepted: list[ProactiveCandidate] = []
        seen_keys: set[str] = set()
        for candidate in candidates:
            if candidate.not_before is not None and candidate.not_before > now:
                continue
            dedupe_key = (candidate.dedupe_key or candidate.candidate_id).strip()
            if dedupe_key in seen_keys:
                continue
            if self._delivery_log.was_recently_sent(
                dedupe_key=dedupe_key,
                now=now,
                within_seconds=self._config.cooldown_seconds,
            ):
                continue
            seen_keys.add(dedupe_key)
            accepted.append(candidate)
        if not accepted:
            return [], "duplicate_recent_send"
        accepted.sort(
            key=lambda item: (
                -item.priority,
                item.not_before or datetime.min.replace(tzinfo=UTC),
                item.candidate_id,
            )
        )
        limited = accepted[: self._config.candidate_limit]
        return limited[: self._config.max_sends_per_tick], "candidate_rejected"


class ProactiveDeliveryService:
    def __init__(self, *, telegram_bot: TelegramBot, chat_id: int) -> None:
        self._telegram_bot = telegram_bot
        self._chat_id = chat_id

    def send(self, *, text: str) -> None:
        self._telegram_bot.send_proactive_message(chat_id=self._chat_id, text=text)


class ProactiveScheduler:
    def __init__(
        self,
        *,
        config: ProactiveConfig,
        plugin_host: PluginHost,
        memory_store: MemoryStore,
        telegram_bot: TelegramBot,
        runtime_state: ProactiveRuntimeState | None = None,
        judge: ProactiveJudge | None = None,
        delivery_log: ProactiveDeliveryLog | None = None,
    ) -> None:
        self._config = config
        self._plugin_host = plugin_host
        self._memory_store = memory_store
        self._telegram_bot = telegram_bot
        self._runtime_state = runtime_state or ProactiveRuntimeState()
        self._judge = judge or ProactiveJudge()
        self._delivery_log = delivery_log or ProactiveDeliveryLog(
            path=self._config.delivery_log_path
        )
        self._collector = ProactiveCandidateCollector(plugin_host=self._plugin_host)
        self._filter = ProactiveCandidateFilter(
            config=self._config,
            runtime_state=self._runtime_state,
            delivery_log=self._delivery_log,
        )
        self._delivery = ProactiveDeliveryService(
            telegram_bot=self._telegram_bot,
            chat_id=self._config.chat_id,
        )
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def runtime_state(self) -> ProactiveRuntimeState:
        return self._runtime_state

    def start(self) -> None:
        if not self._config.enabled or self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self.run_forever, name="proactive-scheduler", daemon=True)
        self._thread.start()
        logger.info("Started proactive scheduler")

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1)
        self._thread = None
        logger.info("Stopped proactive scheduler")

    def run_forever(self) -> None:
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                self.run_tick()
            except Exception:
                logger.exception("Proactive tick failed")
            elapsed = time.monotonic() - started
            remaining = max(0.0, self._config.tick_interval_seconds - elapsed)
            self._stop_event.wait(remaining)

    def run_tick(self) -> ProactiveDecision:
        now = datetime.now(UTC)
        self._runtime_state.last_tick_at = now
        snapshot = self._load_memory_snapshot()
        context = ProactiveContext(
            chat_id=self._config.chat_id,
            now=now,
            last_user_message_at=self._runtime_state.last_user_message_at,
            last_proactive_send_at=self._runtime_state.last_proactive_send_at,
            memory_snapshot=snapshot,
            available_tools=self._plugin_host.available_tools,
            enabled_plugin_ids=self._plugin_host.loaded_plugin_ids,
        )
        candidates = self._collector.collect(context=context)
        logger.info(
            "Collected proactive candidates",
            extra={"chat_id": self._config.chat_id, "candidate_count": len(candidates)},
        )
        filtered, reason = self._filter.apply(candidates=candidates, now=now)
        if not filtered:
            decision = ProactiveDecision(action="skip", reason=reason)
            self._record_outcome(at=now, decision=decision, result="skipped")
            self._runtime_state.consecutive_empty_ticks += 1
            return decision
        try:
            decision = self._judge.decide(candidates=filtered)
        except Exception:
            logger.exception("Proactive judge failed")
            decision = ProactiveDecision(action="skip", reason="judge_failed")
            self._record_outcome(at=now, decision=decision, result="skipped")
            return decision
        if decision.action != "send" or decision.candidate is None or not decision.message_text:
            self._record_outcome(at=now, decision=decision, result="skipped")
            if decision.reason == "no_candidates":
                self._runtime_state.consecutive_empty_ticks += 1
            return decision
        try:
            self._delivery.send(text=decision.message_text)
        except (TelegramAPIError, OSError):
            logger.exception("Failed to send proactive Telegram message")
            self._record_outcome(at=now, decision=decision, result="delivery_failed")
            return decision
        self._runtime_state.last_proactive_send_at = now
        self._runtime_state.consecutive_empty_ticks = 0
        self._record_outcome(at=now, decision=decision, result="sent")
        return decision

    def _load_memory_snapshot(self) -> MemorySnapshot | None:
        try:
            return self._memory_store.load_snapshot()
        except MemoryStoreError:
            logger.exception("Failed to load memory for proactive scheduler")
            return None

    def _record_outcome(
        self,
        *,
        at: datetime,
        decision: ProactiveDecision,
        result: str,
    ) -> None:
        try:
            self._delivery_log.append(
                at=at,
                chat_id=self._config.chat_id,
                result=result,
                reason=decision.reason,
                candidate=decision.candidate,
            )
        except MemoryStoreError:
            logger.exception("Failed to persist proactive delivery outcome")
