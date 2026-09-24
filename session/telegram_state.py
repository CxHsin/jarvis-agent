"""Durable Telegram channel state, separate from the session event log.

The caller owns polling and delivery.  It must persist an inbound identity before
advancing Telegram's offset, and must not execute a duplicate or an unknown task.
This store never replays work or sends messages on its own.
"""

from __future__ import annotations

import json
import os
import tempfile
from functools import wraps
from threading import RLock
from dataclasses import dataclass
from pathlib import Path
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
            data = json.loads(self.path.read_text(encoding="utf-8"))
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
                        or type(entry.get("notified", False)) is not bool):
                    raise ValueError("invalid inbound identity")
            return data
        except (OSError, ValueError, TypeError) as exc:
            raise TelegramStateError("Telegram channel state cannot be loaded; refusing to replay") from exc

    def _commit(self, data: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                             prefix=".telegram-state-", suffix=".tmp",
                                             delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            # Best effort directory sync on platforms supporting directory handles.
            if os.name != "nt":
                try:
                    fd = os.open(self.directory, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                except OSError:
                    # Not all filesystems permit fsync on a directory.
                    pass
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

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
    def receive(self, update_id: int, message_id: int, chat_id: int) -> bool:
        """Persist receipt before acknowledging offset; return True only for new work.

        If a message arrives under a second update identity, it is still a
        duplicate. Identity conflicts are rejected instead of invoking a tool.
        """
        key = self._check_chat(chat_id)
        if (type(update_id) is not int or update_id < 0
                or type(message_id) is not int or message_id <= 0):
            raise ValueError("invalid Telegram update/message ID")
        self._require_open()
        updates = self._data["updates"]
        existing = updates.get(str(update_id))
        if existing is not None:
            if (existing["chat_id"], existing["message_id"]) != (chat_id, message_id):
                raise TelegramStateError("Conflicting update identity")
            return False
        for old_id, entry in updates.items():
            if (entry["chat_id"], entry["message_id"]) == (chat_id, message_id):
                # Persist alias so offset can be advanced for this delivery too.
                self._change(lambda data: data["updates"].__setitem__(str(update_id), dict(entry)))
                return False
        session_id = self._data["bindings"].get(key)
        if session_id is None:
            raise TelegramStateError("Bind a workspace session before receiving tasks")
        entry = {"chat_id": chat_id, "message_id": message_id,
                 "session_id": session_id, "status": "received"}
        self._change(lambda data: data["updates"].__setitem__(str(update_id), entry))
        return True

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
    def acknowledge(self, update_id: int) -> int:
        """Return durable next polling offset; call only after receive()."""
        self._require_open()
        if type(update_id) is not int or str(update_id) not in self._data["updates"]:
            raise TelegramStateError("Cannot acknowledge an unrecorded update")
        next_offset = update_id + 1
        if next_offset > self.offset:
            self._change(lambda data: data.__setitem__("offset", next_offset))
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
                    result.append({"update_id": int(update_id), "chat_id": item["chat_id"]})
            return result

    def mark_notified(self, update_id: int) -> None:
        """Mark notification only after Telegram accepted a reply; never repeats work."""
        with self._mutex:
            self._require_open()
            entry = self._data["updates"].get(str(update_id))
            if entry is None or entry["status"] != "completed":
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
                 "status": item.status} for item in self.unknown()]

    @synchronized
    def unknown(self) -> list[Inbound]:
        """Unique unknown messages for caller to report (no automatic replay)."""
        self._require_open()
        seen = set()
        result = []
        for update_id, item in self._data["updates"].items():
            identity = (item["chat_id"], item["message_id"])
            if item["status"] == "unknown" and identity not in seen:
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
