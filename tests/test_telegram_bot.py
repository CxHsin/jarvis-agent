"""Transport-facing acceptance tests: no network or real credentials."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from session.telegram_state import TelegramState
from telegram_bot import TelegramBot, TelegramTransport, TelegramTransportError, main


class State:
    def __init__(self):
        self.seen = set()
        self.bindings = {}
        self.status = {}
        self.offset = 0

    def receive(self, update_id, message_id, chat_id):
        key = (chat_id, message_id)
        if key in self.seen:
            return False
        self.seen.add(key)
        self.status[update_id] = "received"
        self.offset = max(self.offset, update_id + 1)
        return True

    def acknowledge(self, update_id):
        self.offset = max(self.offset, update_id + 1)
        return self.offset

    def close(self):
        pass

    def mark_started(self, update_id):
        self.status[update_id] = "started"

    def mark_finished(self, update_id, status):
        self.status[update_id] = status

    def session_id(self, chat_id):
        return self.bindings.get(chat_id)

    def bind(self, chat_id, session_id):
        self.bindings[chat_id] = session_id

    def pending_unknown(self):
        return []

    def pending_delivery(self):
        return []

    def mark_notified(self, update_id):
        pass


class Transport:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))


class Session:
    def __init__(self, app, session_id):
        self.app = app
        self.store = type("Store", (), {"session_id": session_id})()

    def run_request(self, text, *, cancellation):
        self.app.calls.append(text)
        if self.app.started is not None:
            self.app.started.set()
            self.app.release.wait(2)
        return "回答：" + text

    def close(self):
        pass


class App:
    def __init__(self, block=False):
        self.calls = []
        self.started = threading.Event() if block else None
        self.release = threading.Event()
        self.created = 0

    def create_session(self, *, resume=None):
        if resume is None:
            self.created += 1
            resume = str(self.created)
        return Session(self, resume)

    def close(self):
        pass


def update(num, text="hello", *, user=42, kind="private", chat_id=42):
    return {"update_id": num, "message": {"message_id": num, "chat": {"id": chat_id, "type": kind},
                                            "from": {"id": user}, "text": text}}


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


def test_private_authorization_and_deduplication(tmp_path):
    app, state, transport = App(), State(), Transport()
    state.bindings[42] = "existing"
    bot = TelegramBot(object(), 42, transport, application=app, state=state)
    try:
        bot.process_update(update(1, user=100))
        bot.process_update(update(2, kind="group"))
        bot.process_update(update(5, chat_id=999))
        bot.process_update(update(3, "", user=42))
        assert not app.calls
        bot.process_update(update(4))
        wait_for(lambda: state.status.get(4) == "completed")
        bot.process_update(update(4))
        assert app.calls == ["hello"]
        assert app.created == 0
        assert any("回答" in text for _, text in transport.sent)
    finally:
        bot.close()


def test_serial_queue_and_cancel_priority():
    app, state, transport = App(block=True), State(), Transport()
    state.bindings[42] = "existing"
    bot = TelegramBot(object(), 42, transport, application=app, state=state)
    try:
        bot.process_update(update(1, "first"))
        assert app.started.wait(2)
        bot.process_update(update(2, "second"))
        assert app.calls == ["first"]
        bot.process_update(update(3, "/cancel"))
        assert bot._cancel.is_set()
        app.release.set()
        wait_for(lambda: state.status.get(2) == "completed")
        assert app.calls == ["first", "second"]
        assert any("排队" in text for _, text in transport.sent)
    finally:
        app.release.set()
        bot.close()


def test_real_state_workspace_binding_and_recovery(tmp_path):
    config = SimpleNamespace(root_dir=tmp_path / "one", state_dir=tmp_path / "state", recent_task_count=5)
    config.root_dir.mkdir()
    transport, app = Transport(), App()
    with TelegramState(config, allowed_user_id=42) as state:
        bot = TelegramBot(config, 42, transport, application=app, state=state)
        bot.process_update(update(8, "first"))
        wait_for(lambda: state.offset == 9 and app.calls == ["first"])
        session_id = state.session_id(42)
        bot.close()
        assert session_id
    with TelegramState(config, allowed_user_id=42) as state:
        assert state.session_id(42) == session_id
        assert not state.receive(8, 8, 42)
        bot = TelegramBot(config, 42, transport, application=app, state=state)
        bot.process_update(update(9, "second"))
        wait_for(lambda: app.calls == ["first", "second"])
        bot.close()
    other = SimpleNamespace(root_dir=tmp_path / "two", state_dir=config.state_dir, recent_task_count=5)
    other.root_dir.mkdir()
    with TelegramState(other, allowed_user_id=42) as state:
        assert state.session_id(42) is None


def test_inflight_duplicate_is_not_replayed_after_restart(tmp_path):
    config = SimpleNamespace(root_dir=tmp_path / "workspace", state_dir=tmp_path / "state", recent_task_count=5)
    config.root_dir.mkdir()
    app, transport = App(), Transport()
    with TelegramState(config, allowed_user_id=42) as state:
        bot = TelegramBot(config, 42, transport, application=app, state=state)
        bot.process_update(update(12, "first"))
        wait_for(lambda: app.calls == ["first"])
        bot.close()
    with TelegramState(config, allowed_user_id=42) as state:
        assert not state.receive(12, 12, 42)
        state.receive(13, 13, 42)
        # Received but not started before a crash must never be replayed.
    with TelegramState(config, allowed_user_id=42) as state:
        assert not state.receive(13, 13, 42)
        assert any(item["update_id"] == 13 for item in state.pending_unknown())


def test_workspace_hardlink_to_external_file_is_rejected(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "alias.txt"
    try:
        import os
        os.link(outside, link)
    except OSError:
        pytest.skip("Filesystem cannot create a hardlink")
    cfg = SimpleNamespace(root_dir=workspace, state_dir=tmp_path / "state",
                          text_extensions=(".txt",), max_read_chars=1000, max_tool_result_tokens=8192)
    from tools.workspace import Workspace, WorkspaceError
    target = Workspace(cfg)
    with pytest.raises(WorkspaceError, match="硬链接"):
        target.read("alias.txt")
    with pytest.raises(WorkspaceError, match="硬链接"):
        target.write("alias.txt", "changed")
    assert outside.read_text(encoding="utf-8") == "secret"


def test_completion_delivery_gap_is_reported_without_reexecution(tmp_path):
    config = SimpleNamespace(root_dir=tmp_path / "workspace", state_dir=tmp_path / "state", recent_task_count=5)
    config.root_dir.mkdir()
    app, transport = App(), Transport()
    with TelegramState(config, allowed_user_id=42) as state:
        bot = TelegramBot(config, 42, transport, application=app, state=state)
        original_mark_notified = state.mark_notified
        # Simulate a crash after completing the task but before delivery was recorded.
        state.mark_notified = lambda update_id: None
        bot.process_update(update(20, "first"))
        wait_for(lambda: app.calls == ["first"] and state.pending_delivery())
        bot.close()
        state.mark_notified = original_mark_notified
    with TelegramState(config, allowed_user_id=42) as state:
        assert len(state.pending_delivery()) == 1
        assert not state.receive(20, 20, 42)
        assert app.calls == ["first"]
        entry = state.pending_delivery()[0]
        state.mark_notified(entry["update_id"])
        assert state.pending_delivery() == []


def test_bot_rejects_token_file_inside_workspace(tmp_path, monkeypatch, capsys):
    secret = tmp_path / ".env"
    secret.write_text("TELEGRAM_BOT_TOKEN=MY_SECRET\nTELEGRAM_ALLOWED_USER_ID=42\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert main(["--env-file", str(secret)]) == 2
    assert "MY_SECRET" not in capsys.readouterr().err


def test_transport_sanitizes_secret(monkeypatch):
    def failing(*args, **kwargs):
        raise OSError("https://api.telegram.org/botMY_SECRET/sendMessage")
    monkeypatch.setattr("urllib.request.urlopen", failing)
    transport = TelegramTransport("MY_SECRET")
    try:
        transport.send(1, "hi")
        assert False, "must fail"
    except TelegramTransportError as exc:
        assert "MY_SECRET" not in str(exc)
        assert exc.__cause__ is None
