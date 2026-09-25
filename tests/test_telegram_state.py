"""Isolated channel-state tests: no Bot, network, credentials or Agent."""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from session.session_store import SessionLockedError, SessionNotFoundError, SessionStore
from session.telegram_state import TelegramState, TelegramStateError


class TelegramStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "one"
        self.root.mkdir()
        self.config = SimpleNamespace(root_dir=self.root, state_dir=self.base / "state",
                                      telegram_user_id=12345, recent_task_count=5)

    def make_session(self, config=None):
        with SessionStore.create(config or self.config) as store:
            return store.session_id

    def test_workspace_binding_survives_restart_and_does_not_change_session_listing(self):
        first = self.make_session()
        with TelegramState(self.config) as state:
            self.assertIsNone(state.session_id(12345))
            state.bind(12345, first)
            self.assertEqual(state.session_id(12345), first)
        other_root = self.base / "two"
        other_root.mkdir()
        other = SimpleNamespace(root_dir=other_root, state_dir=self.config.state_dir,
                                telegram_user_id=12345, recent_task_count=5)
        with TelegramState(other) as state:
            self.assertIsNone(state.session_id(12345))
            with self.assertRaises(SessionNotFoundError):
                state.bind(12345, first)
            second = self.make_session(other)
            state.bind(12345, second)
        with TelegramState(self.config) as state:
            self.assertEqual(state.session_id(12345), first)
        self.assertEqual([s.id for s in SessionStore.list_sessions(self.config)], [first])
        with SessionStore.resume(self.config, first) as store:
            self.assertEqual(store.session_id, first)
        self.assertEqual([s.id for s in SessionStore.list_sessions(other)], [second])

    def test_durable_receipt_offset_dedup_and_completion(self):
        session_id = self.make_session()
        with TelegramState(self.config) as state:
            state.bind(12345, session_id)
            self.assertTrue(state.receive(11, 42, 12345))
            self.assertEqual(state.offset, 0)
            self.assertEqual(state.acknowledge(11), 12)
            state.mark_started(11)
            state.mark_finished(11, "completed")
            self.assertFalse(state.receive(11, 42, 12345))
            self.assertFalse(state.receive(12, 42, 12345))  # second update, same message
            self.assertEqual(state.acknowledge(12), 13)
            self.assertEqual(state.pending_unknown(), [])
        with TelegramState(self.config) as state:
            self.assertEqual(state.offset, 13)
            self.assertFalse(state.receive(11, 42, 12345))
            self.assertFalse(state.receive(12, 42, 12345))
            self.assertEqual(state.pending_unknown(), [])

    def test_received_or_started_are_unknown_on_restart_not_replayed(self):
        session_id = self.make_session()
        with TelegramState(self.config) as state:
            state.bind(12345, session_id)
            self.assertTrue(state.receive(21, 1, 12345))
            state.acknowledge(21)
            self.assertTrue(state.receive(22, 2, 12345))
            state.mark_started(22)
        with TelegramState(self.config) as state:
            self.assertEqual(state.offset, 22)
            self.assertFalse(state.receive(21, 1, 12345))
            self.assertFalse(state.receive(22, 2, 12345))
            self.assertEqual({item["message_id"] for item in state.pending_unknown()}, {1, 2})
            with self.assertRaises(TelegramStateError):
                state.mark_started(22)
            with self.assertRaises(TelegramStateError):
                state.mark_finished(22, "completed")
        with TelegramState(self.config) as state:
            self.assertEqual(len(state.pending_unknown()), 2)

    def test_failed_commit_before_offset_does_not_claim_or_ack(self):
        self.make_session()
        with TelegramState(self.config) as state:
            state.bind(12345, SessionStore.list_sessions(self.config)[0].id)
            with patch.object(state, "_commit", side_effect=OSError("injected crash")):
                with self.assertRaises(OSError):
                    state.receive(31, 3, 12345)
            self.assertEqual(state.offset, 0)
            self.assertTrue(state.receive(31, 3, 12345))
            with patch.object(state, "_commit", side_effect=OSError("injected crash")):
                with self.assertRaises(OSError):
                    state.acknowledge(31)
        with TelegramState(self.config) as state:
            self.assertEqual(state.offset, 0)
            self.assertFalse(state.receive(31, 3, 12345))
            self.assertEqual(state.pending_unknown()[0]["message_id"], 3)
            state.acknowledge(31)

    def test_failed_start_write_blocks_execution_and_recovers_unknown(self):
        self.make_session()
        with TelegramState(self.config) as state:
            state.bind(12345, SessionStore.list_sessions(self.config)[0].id)
            state.receive(50, 5, 12345)
            with patch.object(state, "_commit", side_effect=OSError("injected crash")):
                with self.assertRaises(OSError):
                    state.mark_started(50)
        with TelegramState(self.config) as state:
            self.assertEqual(state.pending_unknown()[0]["update_id"], 50)
            self.assertFalse(state.receive(50, 5, 12345))

    def test_concurrent_polling_and_worker_updates_keep_all_receipts(self):
        session_id = self.make_session()
        with TelegramState(self.config) as state:
            state.bind(12345, session_id)
            def deliver(index):
                self.assertTrue(state.receive(index, index + 1, 12345))
                state.acknowledge(index)
                state.mark_started(index)
                state.mark_finished(index, "completed")
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(deliver, range(1, 101)))
            self.assertEqual(state.offset, 101)
        with TelegramState(self.config) as state:
            self.assertEqual(state.offset, 101)
            self.assertEqual(state.pending_unknown(), [])
            for index in range(1, 101):
                self.assertFalse(state.receive(index, index + 1, 12345))

    def test_rejection_and_exclusive_lock(self):
        first = self.make_session()
        with TelegramState(self.config) as state:
            state.bind(12345, first)
            with self.assertRaises(SessionLockedError):
                TelegramState(self.config)
            for chat_id in (12346, -1, True):
                with self.assertRaises(TelegramStateError):
                    state.session_id(chat_id)
                with self.assertRaises(TelegramStateError):
                    state.receive(99, 1, chat_id)
            with self.assertRaises(TelegramStateError):
                state.acknowledge(99)
            state.receive(100, 7, 12345)
            with self.assertRaises(TelegramStateError):
                state.receive(100, 8, 12345)
            state.mark_started(100)
            state.mark_finished(100, "cancelled")
            self.assertEqual(state.pending_unknown()[0]["status"], "unknown")
        with self.assertRaises(ValueError):
            TelegramState(SimpleNamespace(root_dir=self.root, state_dir=self.config.state_dir))

    def test_state_can_commit_during_active_windows_shell(self):
        import os
        if os.name != "nt":
            self.skipTest("Windows AppContainer integration")
        import time
        from tools.shell_sandbox import start_shell
        from session.session_store import resolve_state_dir
        self.make_session()
        with TelegramState(self.config) as state:
            state.bind(12345, SessionStore.list_sessions(self.config)[0].id)
            with tempfile.TemporaryFile() as output:
                shell = start_shell("Start-Sleep -Seconds 6", self.root, resolve_state_dir(self.config), output)
                try:
                    shell.start()
                    self.assertIsNone(shell.poll())
                    self.assertTrue(state.receive(80, 8, 12345))
                    state.acknowledge(80)
                    state.mark_started(80)
                    state.mark_finished(80, "cancelled")
                    self.assertEqual(state.offset, 81)
                finally:
                    shell.close()
        with TelegramState(self.config) as state:
            self.assertFalse(state.receive(80, 8, 12345))
            self.assertEqual(state.pending_unknown()[0]["status"], "unknown")

    def test_corrupted_index_fails_closed_and_preserves_bytes(self):
        session_id = self.make_session()
        with TelegramState(self.config) as state:
            state.bind(12345, session_id)
            path = state.path
        path.write_text('{"broken":', encoding="utf-8")
        with self.assertRaises(TelegramStateError):
            TelegramState(self.config)
        self.assertEqual(path.read_text(encoding="utf-8"), '{"broken":')


if __name__ == "__main__":
    unittest.main()
