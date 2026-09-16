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
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from task_history import TaskHistory
from memory_service import MemoryService


SESSION_FORMAT_VERSION = 1
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
    header = _first_record(path)
    if not header or header.get("type") != RECORD_SESSION:
        return None
    message_count = 0
    last_user_text = ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, Mapping) or record.get("type") != RECORD_MESSAGE:
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


class SessionStore:
    """Append-only durable log for one Jarvis session."""

    def __init__(self, config: Any, session_id: str, started_at: str):
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

    @property
    def recent_path(self) -> Path:
        return self.history.recent_path

    @classmethod
    def create(cls, config: Any) -> "SessionStore":
        moment = datetime.now()
        store = cls(config, new_session_id(moment), moment.isoformat(timespec="milliseconds"))
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
    def resume(cls, config: Any, session_id: str | None = None) -> "SessionStore":
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
            self._file = open(self.path, "a+", encoding="utf-8")
            self.memory = MemoryService(self.memory_directory)
            self.history = TaskHistory(self.memory_directory, self.session_id, self._recent_task_count, self.memory)
        except OSError:
            self._lock.release()
            raise

    def close(self) -> None:
        if hasattr(self, "memory"):
            self.memory.close()
        if self._file is not None:
            self._file.close()
            self._file = None
        self._lock.release()

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
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
        self._file.write(payload + "\n")
        self._file.flush()
        self.history.record(record)
        return self._file.tell()

    def end_task(self, status: str = "completed") -> None:
        if status == "failed":
            self.history.record({"type": "task_end", "status": status})
        else:
            self._append({"type": "task_end", "status": status})

    def recent_messages(self) -> list[dict[str, Any]]:
        return self.history.messages()

    def record_recent_context(self, messages: Sequence[Mapping[str, Any]]) -> None:
        self._append({"type": "recent_context", "messages": list(messages)})

    def record_runtime(self, state: Mapping[str, Any]) -> None:
        self._append({"type": "runtime", "state": dict(state)})

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
        return self._file.seek(0, os.SEEK_END)

    def truncate_to(self, offset: int) -> None:
        if self._file is None:
            raise SessionError("会话记录已关闭。")
        self._file.flush()
        self._file.seek(max(0, int(offset)))
        self._file.truncate()
        self._file.flush()

    def load(self) -> SessionContents:
        contents = SessionContents(self.session_id, str(self.root_dir), self.started_at)
        if not self.path.is_file():
            return contents
        text = self.path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        warnings: list[str] = []
        compact_compressed_ids: list[str] = []
        complete = text.endswith("\n") or text == ""
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                if index == len(lines) - 1 and not complete:
                    warnings.append("已丢弃进程中断时写了一半的最后一条记录。")
                else:
                    warnings.append(f"第 {index + 1} 行无法解析，已跳过。")
                continue
            if not isinstance(record, Mapping):
                warnings.append(f"第 {index + 1} 行不是记录对象，已跳过。")
                continue
            kind = record.get("type")
            if kind == RECORD_SESSION:
                contents.workspace = str(record.get("workspace", contents.workspace))
                contents.started_at = str(record.get("started_at", contents.started_at))
            elif kind == RECORD_MESSAGE:
                message = record.get("message")
                if isinstance(message, Mapping):
                    contents.messages.append(dict(message))
            elif kind == "recent_context":
                contents.messages = [dict(message) for message in record.get("messages", [])]
            elif kind == "runtime":
                contents.runtime = dict(record.get("state") or {})
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
        return contents
