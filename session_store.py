"""Durable session records for Jarvis.

One session is one append-only JSONL file.  Every record is written through as
it happens, so a killed process loses at most the record it was writing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from threading import RLock
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from task_history import TaskHistory


SESSION_FORMAT_VERSION = 2
RECORD_SESSION = "session"
RECORD_MESSAGE = "message"
RECORD_TASK = "task"
RECORD_ARCHIVE = "archive"
RECORD_REPLACE = "replace"
RECORD_COMPACT = "compact"


class SessionError(RuntimeError):
    """Base class for session storage failures."""


class SessionNotFoundError(SessionError):
    """The requested session does not exist in this workspace."""


class SessionLockedError(SessionError):
    """Another process already holds this session."""


def default_state_dir() -> Path:
    """Return the platform location for session records."""

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "jarvis"
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "jarvis"
    return Path.home() / ".local" / "state" / "jarvis"


def resolve_state_dir(config: Any) -> Path:
    configured = getattr(config, "state_dir", None)
    if configured:
        return Path(configured).expanduser()
    return default_state_dir()


def workspace_key(root_dir: Path) -> str:
    """Name the per-workspace directory without leaking the full path."""

    resolved = Path(root_dir).expanduser().resolve()
    normalised = os.path.normcase(str(resolved))
    digest = hashlib.sha1(normalised.encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^0-9A-Za-z._-]+", "-", resolved.name).strip("-")[:40] or "workspace"
    return f"{slug}-{digest}"


def session_directory(config: Any) -> Path:
    return resolve_state_dir(config) / "sessions" / workspace_key(Path(getattr(config, "root_dir")))


def new_session_id(moment: datetime | None = None) -> str:
    stamp = (moment or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:4]}"


@dataclass(frozen=True)
class SessionInfo:
    id: str
    path: Path
    workspace: str
    started_at: str
    message_count: int
    last_user_text: str


@dataclass
class SessionContents:
    session_id: str
    workspace: str
    started_at: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    archive: list[dict[str, Any]] = field(default_factory=list)
    replacements: dict[str, str] = field(default_factory=dict)
    compressed_call_ids: list[str] = field(default_factory=list)
    task_number: int = 0
    warnings: tuple[str, ...] = ()
    runtime: dict[str, Any] = field(default_factory=dict)
    audit: list[dict[str, Any]] = field(default_factory=list)
    has_checkpoint: bool = False


class _SessionLock:
    """Cross-process lock that the operating system releases on process exit."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise SessionLockedError(
                f"会话已被另一个 Jarvis 实例占用: {self.path.stem}"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()


def _first_record(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return None
                return record if isinstance(record, dict) else None
    except OSError:
        return None
    return None


def _read_info(path: Path) -> SessionInfo | None:
    from session_migration import committed_path
    path = committed_path(path)
    header = _first_record(path)
    if not header or header.get("type") != RECORD_SESSION:
        return None
    message_count = 0
    last_user_text = ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    records, _ = _read_records(text)
    for record in _visible_records(records):
        if record.get("type") != RECORD_MESSAGE:
            continue
        message = record.get("message")
        if not isinstance(message, Mapping):
            continue
        message_count += 1
        if message.get("role") == "user":
            last_user_text = str(message.get("content", ""))
    return SessionInfo(
        id=str(header.get("id") or path.stem),
        path=path,
        workspace=str(header.get("workspace", "")),
        started_at=str(header.get("started_at", "")),
        message_count=message_count,
        last_user_text=last_user_text,
    )


def _read_records(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    records, warnings = [], []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1 and not text.endswith("\n"):
                warnings.append("已忽略进程中断时写了一半的最后一条记录；原始字节保留。")
            else:
                warnings.append(f"第 {index + 1} 行无法解析，已跳过。")
            continue
        if not isinstance(record, dict):
            warnings.append(f"第 {index + 1} 行不是记录对象，已跳过。")
            continue
        records.append(record)
    return records, warnings


def _visible_records(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project context validity without rolling back independent durable facts."""
    visible = []
    for record in records:
        if record.get('migration_projection') == 'trajectory':
            continue
        if record.get("type") in {"context_reset", "context_rollback"}:
            through = record["through_sequence"]
            visible = [event for event in visible if event.get("sequence", 0) <= through
                       or event.get("type") in {"runtime", "tool_audit"}]
            if "runtime" in record:
                visible.append(dict(record, type="runtime", state=record["runtime"]))
        else:
            visible.append(record)
    return visible


class SessionStore:
    """Append-only durable log for one Jarvis session."""

    def __init__(self, config: Any, session_id: str, started_at: str):
        self.config = config
        self.root_dir = Path(getattr(config, "root_dir"))
        self.state_dir = resolve_state_dir(config)
        self.directory = session_directory(config)
        self.session_id = session_id
        self.started_at = started_at
        self.path = self.directory / f"{session_id}.jsonl"
        self._file = None
        self._lock = _SessionLock(self.directory / f"{session_id}.lock")
        self._write_lock = RLock()
        self.memory_directory = self.state_dir / "memory"
        self._recent_task_count = getattr(config, "recent_task_count", 5)
        self.history = None
        self._format_version = SESSION_FORMAT_VERSION
        self._sequence = 0
        self._task_id = None
        self._needs_separator = False
        self._history_dirty = False

    @property
    def recent_path(self) -> Path:
        return self.history.recent_path

    @classmethod
    def create(cls, config: Any, *, memory=None) -> "SessionStore":
        if memory is None:
            from application import standalone_store
            return standalone_store(config)
        moment = datetime.now()
        store = cls(config, new_session_id(moment), moment.isoformat(timespec="milliseconds"))
        if memory is not None:
            store.memory = memory
        store._open()
        store._append(
            {
                "type": RECORD_SESSION,
                "version": SESSION_FORMAT_VERSION,
                "id": store.session_id,
                "workspace": str(store.root_dir),
                "started_at": store.started_at,
            }
        )
        return store

    @classmethod
    def resume(cls, config: Any, session_id: str | None = None, *, memory=None) -> "SessionStore":
        if memory is None:
            from application import standalone_store
            return standalone_store(config, session_id=session_id, resume=True)
        directory = session_directory(config)
        candidates: list[tuple[str, float, str, Path]] = []
        if directory.is_dir():
            for path in directory.glob("*.jsonl"):
                header = _first_record(path)
                if not header or header.get("type") != RECORD_SESSION:
                    continue
                try:
                    modified = path.stat().st_mtime
                except OSError:
                    modified = 0.0
                candidates.append(
                    (str(header.get("started_at", "")), modified, str(header.get("id") or path.stem), path)
                )
        candidates.sort(reverse=True)
        if session_id:
            chosen = next((item for item in candidates if item[2] == session_id), None)
            if chosen is None:
                raise SessionNotFoundError(f"当前工作区找不到会话 {session_id}。")
        elif candidates:
            chosen = candidates[0]
        else:
            raise SessionNotFoundError("当前工作区没有可恢复的会话记录。")
        store = cls(config, chosen[2], chosen[0])
        if memory is not None:
            store.memory = memory
        store._open(existing=True)
        return store

    @classmethod
    def list_sessions(cls, config: Any) -> list[SessionInfo]:
        directory = session_directory(config)
        if not directory.is_dir():
            return []
        infos = [info for info in (_read_info(path) for path in directory.glob("*.jsonl")) if info]
        infos.sort(key=lambda item: (item.started_at, item.id), reverse=True)
        return infos

    def _open(self, existing: bool = False) -> None:
        if existing and not self.path.is_file():
            raise SessionNotFoundError(f"会话记录不存在: {self.path}")
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock.acquire()
        try:
            if existing:
                from session_migration import committed_path, migrate
                self.path = committed_path(self.path)
                header = _first_record(self.path) or {}
                self._format_version = int(header.get("version", 1))
                if self._format_version not in {1, SESSION_FORMAT_VERSION}:
                    raise SessionError(f"不支持的会话版本: {self._format_version}")
                if self._format_version == 1:
                    self.path = migrate(self)
                    self._format_version = SESSION_FORMAT_VERSION
                text = self.path.read_text(encoding="utf-8", errors="replace")
                self._needs_separator = bool(text and not text.endswith("\n"))
                records, _ = _read_records(text)
                for record in records:
                    self._sequence = max(self._sequence, int(record.get("sequence", 0)))
                    if record.get("type") == RECORD_TASK:
                        self._task_id = record.get("task_id")
            self._file = open(self.path, "a+", encoding="utf-8")
            try:
                self._refresh_history()
            except Exception as exc:
                if (self.path.parent / 'committed.json').exists():
                    raise SessionError('迁移已经提交；派生视图重建失败，修复存储问题后再次恢复会话。') from exc
                raise
        except BaseException:
            if self._file is not None:
                self._file.close()
                self._file = None
            self._lock.release()
            raise

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        self._lock.release()

    def _refresh_history(self, *, tolerate_publication_failure=False) -> None:
        self.history = TaskHistory(
            self.memory_directory, self.session_id, self._recent_task_count, self.memory,
            event_path=self.path if self._format_version == SESSION_FORMAT_VERSION else None,
            publish=False,
        )
        try:
            self.history.project()
        except Exception as exc:
            self._history_dirty = True
            if not tolerate_publication_failure:
                raise
            print(f"[会话投影] 原始事件已保存，派生视图等待重建: {exc}", file=sys.stderr)
            return
        self._history_dirty = False

    def __enter__(self) -> "SessionStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _append(self, record: Mapping[str, Any]) -> int:
        with self._write_lock:
            return self._append_locked(record)

    def _append_locked(self, record: Mapping[str, Any]) -> int:
        if self._file is None:
            raise SessionError("会话记录已关闭。")
        if self._format_version == SESSION_FORMAT_VERSION:
            now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            if record["type"] == RECORD_TASK:
                self._task_id = f"{self.session_id}:{uuid.uuid4().hex}"
            record = dict(record, event_version=1, event_id=uuid.uuid4().hex,
                          session_id=self.session_id, task_id=self._task_id,
                          sequence=self._sequence + 1, recorded_at=now, occurred_at=None)
            if record["type"] == RECORD_TASK:
                record["recent_task_count"] = self._recent_task_count
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
        if self._needs_separator:
            self._file.write("\n")
            self._needs_separator = False
        self._file.write(payload + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())
        self._sequence = int(record.get("sequence", self._sequence))
        if self._format_version == SESSION_FORMAT_VERSION:
            try:
                if self._history_dirty:
                    self._refresh_history()
                else:
                    self.history.record(record)
            except Exception as exc:
                # The canonical commit succeeded. A derived-view error must not
                # make callers leave live state behind the recoverable state.
                self._history_dirty = True
                print(f"[会话投影] 原始事件已保存，派生视图等待重建: {exc}", file=sys.stderr)
        else:
            self.history.record(record)
        return self._file.tell()

    def end_task(self, status: str = "completed", *, context_valid: bool | None = None) -> None:
        record = {"type": "task_end", "status": status,
                  "context_valid": status != "failed" if context_valid is None else context_valid}
        if status == "failed" and self._format_version == 1:
            self.history.record(record)
        else:
            self._append(record)

    def recent_messages(self) -> list[dict[str, Any]]:
        if self._history_dirty:
            # Only publication may fail open; a failed canonical read must not
            # silently substitute stale conversation state.
            self._refresh_history(tolerate_publication_failure=True)
        return self.history.messages()

    def context_messages(self) -> list[dict[str, Any]]:
        """Keep committed compaction authoritative over the raw Recent view."""
        contents = self.load()
        return contents.messages if contents.has_checkpoint else self.recent_messages()

    def record_recent_context(self, messages: Sequence[Mapping[str, Any]]) -> None:
        if self._format_version == SESSION_FORMAT_VERSION:
            # A checkpoint is already durably authoritative. Never replace it
            # with full task evidence from the independent Recent projection.
            if self.load().has_checkpoint:
                return
            if list(messages) != self.recent_messages():
                raise ValueError('Recent context must select canonical messages')
            self._append({'type': 'recent_context',
                          'message_event_ids': self.history.message_event_ids()})
        else:
            self._append({"type": "recent_context", "messages": list(messages)})

    def record_runtime(self, state: Mapping[str, Any], audit: Mapping[str, Any] | None = None) -> None:
        record = {"type": "runtime", "state": dict(state)}
        if audit is not None:
            record["audit"] = dict(audit)
        self._append(record)

    def record_tool_audit(self, event: Mapping[str, Any]) -> None:
        self._append({"type": "tool_audit", "event": dict(event)})

    def record_message(self, message: Mapping[str, Any]) -> None:
        self._append({"type": RECORD_MESSAGE, "message": dict(message)})

    def record_task(self, number: int, goal: str) -> None:
        self._append({"type": RECORD_TASK, "number": int(number), "goal": str(goal)})

    def record_archive(self, entry: Mapping[str, Any]) -> None:
        payload = dict(entry)
        payload["arguments"] = dict(entry.get("arguments") or {})
        payload["result"] = dict(entry.get("result") or {})
        self._append({"type": RECORD_ARCHIVE, "entry": payload})

    def record_compact(
        self,
        kept_from: int,
        content: str,
        method: str,
        compressed_call_ids: Sequence[str] = (),
        reason: str = "auto",
    ) -> None:
        self._append(
            {
                "type": RECORD_COMPACT,
                "kept_from": int(kept_from),
                "content": str(content),
                "method": str(method),
                "compressed_call_ids": [str(call_id) for call_id in compressed_call_ids],
                "reason": str(reason),
            }
        )

    def mark(self) -> int:
        if self._file is None:
            raise SessionError("会话记录已关闭。")
        self._file.flush()
        if self._format_version == SESSION_FORMAT_VERSION:
            return self._sequence
        return self._file.seek(0, os.SEEK_END)

    def truncate_to(self, offset: int) -> None:
        if self._file is None:
            raise SessionError("会话记录已关闭。")
        if self._format_version == SESSION_FORMAT_VERSION:
            self._append({"type": "context_reset", "through_sequence": int(offset)})
            return
        self._file.flush()
        self._file.seek(max(0, int(offset)))
        self._file.truncate()
        self._file.flush()

    def rollback_context(self, boundary: int, runtime: Mapping[str, Any],
                         audit: Sequence[Mapping[str, Any]] = ()) -> None:
        """Commit context exclusion and retained runtime as a single transition."""
        if self._format_version == 1:
            self.truncate_to(boundary)
            self.record_runtime(runtime)
            for event in audit:
                self.record_tool_audit(event)
            return
        self._append({"type": "context_rollback", "through_sequence": int(boundary),
                      "reason": "model_failure", "runtime": dict(runtime)})

    def load(self) -> SessionContents:
        contents = SessionContents(self.session_id, str(self.root_dir), self.started_at)
        if not self.path.is_file():
            return contents
        text = self.path.read_text(encoding="utf-8", errors="replace")
        records, warnings = _read_records(text)
        original_messages = {record['event_id']: record['message'] for record in records
                             if record.get('type') == RECORD_MESSAGE and 'event_id' in record}
        compact_compressed_ids: list[str] = []
        for record in _visible_records(records):
            kind = record.get("type")
            if kind == RECORD_SESSION:
                contents.workspace = str(record.get("workspace", contents.workspace))
                contents.started_at = str(record.get("started_at", contents.started_at))
            elif kind == RECORD_MESSAGE:
                message = record.get("message")
                if isinstance(message, Mapping):
                    contents.messages.append(dict(message))
            elif kind == "recent_context":
                if 'message_event_ids' in record:
                    contents.messages = [dict(original_messages[event_id])
                                         for event_id in record['message_event_ids']]
                else:
                    contents.messages = [dict(message) for message in record.get("messages", [])]
                contents.has_checkpoint = False
            elif kind == "runtime":
                contents.runtime = dict(record.get("state") or {})
                if record.get("audit"):
                    contents.audit.append(dict(record["audit"]))
            elif kind == "tool_audit":
                contents.audit.append(dict(record.get("event") or {}))
            elif kind == RECORD_ARCHIVE:
                entry = record.get("entry")
                if isinstance(entry, Mapping):
                    contents.archive.append(dict(entry))
            elif kind == RECORD_REPLACE:
                call_id = record.get("tool_call_id")
                if isinstance(call_id, str):
                    contents.replacements[call_id] = str(record.get("content", ""))
            elif kind == RECORD_COMPACT:
                contents.has_checkpoint = True
                kept_from = int(record.get("kept_from") or 1)
                kept_log_index = max(0, kept_from - 1)
                checkpoint = {"role": "user", "content": str(record.get("content", ""))}
                contents.messages = [checkpoint] + contents.messages[kept_log_index:]
                for call_id in record.get("compressed_call_ids") or []:
                    compact_compressed_ids.append(str(call_id))
            elif kind == RECORD_TASK:
                try:
                    contents.task_number = max(contents.task_number, int(record.get("number") or 0))
                except (TypeError, ValueError):
                    continue
        contents.compressed_call_ids = list(contents.replacements) + compact_compressed_ids
        for message in contents.messages:
            if message.get("role") != "tool":
                continue
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id in contents.replacements:
                message["content"] = contents.replacements[call_id]
        if contents.task_number == 0:
            for entry in contents.archive:
                try:
                    contents.task_number = max(contents.task_number, int(entry.get("task") or 0))
                except (TypeError, ValueError):
                    continue
        contents.warnings = tuple(warnings)
        report_path = self.path.parent / 'committed.json'
        if report_path.exists():
            report = json.loads(report_path.read_text(encoding='utf-8'))
            contents.warnings += tuple(report.get('warnings', []))
            contents.warnings += tuple(f"迁移冲突已保留双方: {item}" for item in report.get('conflicts', []))
        return contents
