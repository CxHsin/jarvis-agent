"""Durable Telegram delivery coordinator, separate from the session event log.

The caller owns filtering, polling, queueing and transport. This module commits
delivery identity with the offset, then orders execution and notification around
durable state transitions. It never replays work or stores message text.
"""

from __future__ import annotations

import json
import os
from functools import wraps
from threading import RLock
from dataclasses import dataclass
from typing import Any

from session.session_store import (
    SessionNotFoundError,
    SessionStore,
    _SessionLock,
    session_directory,
    workspace_key,
)


class TelegramStateError(RuntimeError):
    """Invalid or unavailable channel state; fail closed rather than retry work."""


def synchronized(method):
    """Serialize channel operations within a process as well as across processes."""
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._mutex:
            return method(self, *args, **kwargs)
    return wrapper


@dataclass(frozen=True)
class Inbound:
    update_id: int
    chat_id: int
    message_id: int
    session_id: str
    status: str  # received, started, completed, unknown
    is_new: bool = False


class TelegramState:
    """One exclusively locked, per-workspace channel index.

    Instantiate with the configured private user ID (which is also the private
    chat ID); never pass a Bot token.  Only metadata is stored, not message text.
    Open once per polling worker, close on shutdown.  On reopening, any received
    or started work is made unknown; caller must report it, never auto-replay it.
    """

    VERSION = 1

    def __init__(self, config: Any, *, allowed_user_id: int | None = None):
        if allowed_user_id is None:
            allowed_user_id = getattr(config, "telegram_user_id", None)
        if type(allowed_user_id) is not int or allowed_user_id <= 0:
            raise ValueError("allowed_user_id must be a positive numeric Telegram user ID")
        self.config = config
        self._mutex = RLock()
        self.allowed_user_id = allowed_user_id
        self.directory = session_directory(config)
        self.path = self.directory / "telegram-state.json"
        self._lock = _SessionLock(self.directory / "telegram-state.lock")
        self._closed = False
        self._lock.acquire()
        try:
            self._data = self._load()
            # Recovery is durable before the caller may consume another update.
            changed = False
            for entry in self._data["updates"].values():
                if entry["status"] in ("received", "started"):
                    entry["status"] = "unknown"
                    changed = True
            if changed:
                self._commit(self._data)
        except BaseException:
            self._lock.release()
            raise

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": self.VERSION, "workspace_key": workspace_key(self.config.root_dir),
                    "bindings": {}, "updates": {}, "offset": 0}
        try:
            records = self.path.read_bytes().splitlines(keepends=True)
            if not records:
                raise ValueError("empty channel state")
            # The last append may have been torn by a crash; only complete,
            # newline-terminated snapshots can be used as committed state.
            if not records[-1].endswith(b"\n"):
                records.pop()
            if not records:
                raise ValueError("no committed channel state")
            data = json.loads(records[-1].decode("utf-8"))
            if (type(data) is not dict or data.get("version") != self.VERSION
                    or data.get("workspace_key") != workspace_key(self.config.root_dir)
                    or type(data.get("offset")) is not int or data["offset"] < 0
                    or type(data.get("bindings")) is not dict
                    or type(data.get("updates")) is not dict):
                raise ValueError("invalid channel state header")
            if any(not isinstance(key, str) or not isinstance(value, str)
                   for key, value in data["bindings"].items()):
                raise ValueError("invalid bindings")
            for key, entry in data["updates"].items():
                if (not isinstance(key, str) or not key.isdecimal() or type(entry) is not dict
                        or any(type(entry.get(field)) is not int for field in ("chat_id", "message_id"))
                        or not isinstance(entry.get("session_id"), str)
                        or entry.get("status") not in ("received", "started", "completed", "unknown")
                        or entry.get("kind", "task") not in ("task", "control")
                        or type(entry.get("notified", False)) is not bool):
                    raise ValueError("invalid inbound identity")
            return data
        except (OSError, ValueError, TypeError) as exc:
            raise TelegramStateError("Telegram channel state cannot be loaded; refusing to replay") from exc

    def _commit(self, data: dict) -> None:
        """Append a durable snapshot; Windows sandbox ACL handles prohibit replace.

        A complete newline-terminated record is the commit boundary. The prior
        snapshot remains available if an append is torn by process termination.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with open(self.path, "ab+") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    handle.seek(0, os.SEEK_END)
                    handle.write(b"\n")  # separate a torn tail from the next snapshot
            handle.seek(0, os.SEEK_END)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

    @synchronized
    def _change(self, mutate):
        self._require_open()
        candidate = json.loads(json.dumps(self._data))
        result = mutate(candidate)
        self._commit(candidate)
        self._data = candidate
        return result

    def _require_open(self):
        if self._closed:
            raise TelegramStateError("Telegram state is closed")

    def _check_chat(self, chat_id: int) -> str:
        if type(chat_id) is not int or chat_id != self.allowed_user_id:
            raise TelegramStateError("Only the configured user's private chat is allowed")
        return str(chat_id)

    @synchronized
    def session_for(self, chat_id: int) -> str | None:
        self._require_open()
        return self._data["bindings"].get(self._check_chat(chat_id))

    def session_id(self, chat_id: int) -> str | None:
        """Return the bound session ID, or None; never select another workspace's session."""
        return self.session_for(chat_id)

    @synchronized
    def bind(self, chat_id: int, session_id: str) -> None:
        """Bind only an existing session in this workspace; never resume latest implicitly."""
        key = self._check_chat(chat_id)
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id required")
        if session_id not in {info.id for info in SessionStore.list_sessions(self.config)}:
            raise SessionNotFoundError("Session does not exist in the current workspace")
        if self.session_for(chat_id) == session_id:
            return
        self._change(lambda data: data["bindings"].__setitem__(key, session_id))

    @property
    @synchronized
    def offset(self) -> int:
        self._require_open()
        return self._data["offset"]

    @synchronized
    def claim(self, update_id: int, message_id: int, chat_id: int,
              *, kind: str = "task") -> Inbound:
        """Durably claim one delivery and its polling offset in one commit."""
        key = self._check_chat(chat_id)
        if (type(update_id) is not int or update_id < 0
                or type(message_id) is not int or message_id <= 0):
            raise ValueError("invalid Telegram update/message ID")
        if kind not in {"task", "control"}:
            raise ValueError("invalid channel delivery kind")
        self._require_open()
        updates = self._data["updates"]
        existing = updates.get(str(update_id))
        if existing is not None:
            if (existing["chat_id"], existing["message_id"]) != (chat_id, message_id):
                raise TelegramStateError("Conflicting update identity")
            if update_id + 1 > self.offset:
                self._change(lambda data: data.__setitem__("offset", update_id + 1))
            return Inbound(update_id, chat_id, message_id, existing["session_id"], existing["status"])
        duplicate = next((entry for entry in updates.values()
                          if (entry["chat_id"], entry["message_id"]) == (chat_id, message_id)), None)
        if duplicate is not None:
            def record_alias(data):
                data["updates"][str(update_id)] = dict(duplicate)
                data["offset"] = max(data["offset"], update_id + 1)
            self._change(record_alias)
            return Inbound(update_id, chat_id, message_id, duplicate["session_id"], duplicate["status"])
        session_id = self._data["bindings"].get(key)
        if session_id is None:
            with SessionStore.create(self.config) as store:
                session_id = store.session_id
        entry = {"chat_id": chat_id, "message_id": message_id,
                 "session_id": session_id, "status": "received", "kind": kind}
        def record_claim(data):
            data["bindings"][key] = session_id
            data["updates"][str(update_id)] = entry
            data["offset"] = max(data["offset"], update_id + 1)
        self._change(record_claim)
        return Inbound(update_id, chat_id, message_id, session_id, "received", is_new=True)

    @synchronized
    def acknowledge_ignored(self, update_id: int) -> int:
        """Durably advance past a filtered update, without recording untrusted chats.

        Pollers may only call this after the update has been rejected or ignored.
        Persisting it before the next poll avoids keeping an in-memory offset
        that claims more progress than the durable channel index.
        """
        self._require_open()
        if type(update_id) is not int or update_id < 0:
            raise ValueError("invalid Telegram update ID")
        if update_id + 1 > self.offset:
            self._change(lambda data: data.__setitem__("offset", update_id + 1))
        return self.offset

    @synchronized
    def _transition(self, update_id: int, before: str, after: str) -> Inbound:
        self._require_open()
        entry = self._data["updates"].get(str(update_id))
        if entry is None or entry["status"] != before:
            raise TelegramStateError(f"Cannot transition update {update_id} from {before}")
        # A second update for the same Telegram message shares its status.
        chat, message = entry["chat_id"], entry["message_id"]
        def mutate(data):
            for item in data["updates"].values():
                if (item["chat_id"], item["message_id"]) == (chat, message):
                    item["status"] = after
        self._change(mutate)
        return Inbound(update_id, chat, message, entry["session_id"], after)

    def start(self, update_id: int) -> Inbound:
        """Commit started *before* invoking Agent or any side-effectful tool."""
        return self._transition(update_id, "received", "started")

    def complete(self, update_id: int) -> Inbound:
        """Commit completed after task result is durable, before notifying user."""
        return self._transition(update_id, "started", "completed")

    def interrupt(self, update_id: int) -> Inbound:
        """Explicit cancellation/failure after start has unknown side-effect status."""
        return self._transition(update_id, "started", "unknown")

    def mark_started(self, update_id: int) -> None:
        self.start(update_id)

    def mark_finished(self, update_id: int, status: str) -> None:
        """Only successful completion is known; failure/cancel may have side effects."""
        if status == "completed":
            self.complete(update_id)
        elif status in {"failed", "cancelled", "interrupted", "unknown"}:
            self.interrupt(update_id)
        else:
            raise ValueError("Unsupported task status")

    def deliver(self, update_id: int, execute, send, failure_reply: str) -> None:
        """Persist each delivery transition before invoking the next action."""
        inbound = self.start(update_id)
        try:
            status, reply = execute(inbound.session_id)
        except Exception:
            status, reply = "unknown", failure_reply
        self.mark_finished(update_id, status)
        try:
            sent = send(inbound.chat_id, reply)
        except Exception:
            sent = False
        if sent:
            self.mark_notified(update_id)

    def pending_delivery(self) -> list[dict]:
        """Completed tasks whose reply may have been lost after durable completion."""
        with self._mutex:
            self._require_open()
            seen = set()
            result = []
            for update_id, item in self._data["updates"].items():
                identity = (item["chat_id"], item["message_id"])
                if item["status"] == "completed" and not item.get("notified", False) and identity not in seen:
                    seen.add(identity)
                    result.append({"update_id": int(update_id), "chat_id": item["chat_id"],
                                   "kind": item.get("kind", "task")})
            return result

    def mark_notified(self, update_id: int) -> None:
        """Mark notification only after Telegram accepted a reply; never repeats work."""
        with self._mutex:
            self._require_open()
            entry = self._data["updates"].get(str(update_id))
            if entry is None or entry["status"] not in {"completed", "unknown"}:
                raise TelegramStateError("Cannot mark an unfinished update notified")
            chat, message = entry["chat_id"], entry["message_id"]
            def mutate(data):
                for item in data["updates"].values():
                    if (item["chat_id"], item["message_id"]) == (chat, message):
                        item["notified"] = True
            self._change(mutate)

    def pending_unknown(self) -> list[dict]:
        return [{"update_id": item.update_id, "message_id": item.message_id,
                 "chat_id": item.chat_id, "session_id": item.session_id,
                 "status": item.status, "kind": self._data["updates"][str(item.update_id)].get("kind", "task")}
                for item in self.unknown()]

    def notify_pending(self, send, unknown_reply: str, completed_reply: str,
                       control_reply: str = "控制指令回执送达状态未知；请核对当前任务。") -> None:
        """Report recovery outcomes once, without replaying work or original replies."""
        for entries, reply in ((self.pending_unknown(), unknown_reply),
                               (self.pending_delivery(), completed_reply)):
            for entry in entries:
                actual_reply = control_reply if entry.get("kind") == "control" else reply
                try:
                    sent = send(entry["chat_id"], actual_reply)
                except Exception:
                    sent = False
                if sent:
                    self.mark_notified(entry["update_id"])

    @synchronized
    def unknown(self) -> list[Inbound]:
        """Unique unknown messages for caller to report (no automatic replay)."""
        self._require_open()
        seen = set()
        result = []
        for update_id, item in self._data["updates"].items():
            identity = (item["chat_id"], item["message_id"])
            if item["status"] == "unknown" and not item.get("notified", False) and identity not in seen:
                seen.add(identity)
                result.append(Inbound(int(update_id), *identity, item["session_id"], "unknown"))
        return result

    @synchronized
    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._lock.release()

    def __enter__(self) -> "TelegramState":
        return self

    def __exit__(self, *args) -> None:
        self.close()
