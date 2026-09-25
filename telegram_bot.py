"""Single-user Telegram text transport for Jarvis (no implicit privilege elevation)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

from application import Application
from configuration import Config, ConfigurationError, _read_dotenv, _setting
from session.session_store import SessionNotFoundError
from session.telegram_state import TelegramState


class TelegramTransportError(RuntimeError):
    """Deliberately excludes URLs and bodies (which may contain the bot token)."""


class TelegramTransport:
    def __init__(self, token: str):
        if not token:
            raise ValueError("missing bot token")
        self._url = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, **fields: Any) -> Any:
        payload = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(self._url + method, data=payload, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.load(response)
        except (OSError, ValueError, urllib.error.URLError):
            raise TelegramTransportError(f"Telegram {method} request failed") from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise TelegramTransportError(f"Telegram {method} rejected request")
        return result.get("result")

    def poll(self, offset: int) -> list[dict]:
        result = self.call("getUpdates", offset=offset, timeout=20,
                           allowed_updates=json.dumps(["message"]))
        if not isinstance(result, list):
            raise TelegramTransportError("Telegram getUpdates returned invalid result")
        return result

    def send(self, chat_id: int, text: str) -> None:
        # Telegram limits message size; split text without dropping content.
        for start in range(0, len(text), 4000):
            self.call("sendMessage", chat_id=chat_id, text=text[start:start + 4000])


class TelegramBot:
    """Serial worker with high-priority cancellation on the polling thread."""
    def __init__(self, config: Config, user_id: int, transport, *, application=None, state=None,
                 secret: str | None = None):
        self.config, self.user_id, self.transport = config, user_id, transport
        self._secret = secret
        self.application = application or Application(config)
        self._owns_application = application is None
        self.state = state if state is not None else TelegramState(config, allowed_user_id=user_id)
        self._queue: deque[tuple[int, int, str]] = deque()
        self._lock = threading.RLock()
        self._busy = False
        self._cancel: threading.Event | None = None
        self._worker: threading.Thread | None = None
        self._closed = False

    def _safe_send(self, chat_id: int, text: str) -> bool:
        try:
            safe_text = text.replace(self._secret, "[redacted]") if self._secret else text
            self.transport.send(chat_id, safe_text)
            return True
        except Exception:
            # Do not print exception: third-party transports may embed tokens in URLs.
            print("[Telegram] 无法发送消息，送达状态未知。", file=sys.stderr)
            return False

    def process_update(self, update: dict) -> bool:
        """Persist or discard a delivery atomically with respect to shutdown."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Bot is closed")
            return self._process_update(update)

    def _process_update(self, update: dict) -> bool:
        """Filter untrusted metadata; True means the update was durably claimed."""
        message = update.get("message")
        if not isinstance(message, dict):
            return False
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if (chat.get("type") != "private" or type(sender.get("id")) is not int
                or sender["id"] != self.user_id or type(chat.get("id")) is not int):
            return False
        chat_id = chat["id"]
        if chat_id != self.user_id:
            return False
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return False
        if self._secret:
            text = text.replace(self._secret, "[redacted]")
        if type(update.get("update_id")) is not int or type(message.get("message_id")) is not int:
            return False
        update_id, message_id = update["update_id"], message["message_id"]
        with self._lock:
            is_cancel = text.strip().casefold() == "/cancel"
            inbound = self.state.claim(update_id, message_id, chat_id,
                                       kind="control" if is_cancel else "task")
            if not inbound.is_new:
                return True
        if is_cancel:
            def cancel(_session_id):
                with self._lock:
                    cancellation = self._cancel
                if cancellation is not None:
                    cancellation.set()
                reply = ("已请求取消当前任务；已发生的操作不会撤销。" if cancellation is not None
                         else "当前没有运行中的任务。")
                return "completed", reply
            self.state.deliver(update_id, cancel, self._safe_send,
                               "取消指令处理状态未知；请核对当前任务。")
            return True
        with self._lock:
            queued = self._busy or bool(self._queue)
            self._queue.append((update_id, chat_id, text))
            self._launch_next_locked()
        self._safe_send(chat_id, "已排队，稍后处理。" if queued else "已收到，正在处理。")
        return True

    def _launch_next_locked(self):
        if self._closed or self._busy or not self._queue:
            return
        update_id, chat_id, text = self._queue.popleft()
        self._busy = True
        self._cancel = threading.Event()
        worker = threading.Thread(target=self._run_task,
                                  args=(update_id, chat_id, text, self._cancel), daemon=True)
        self._worker = worker
        worker.start()

    def _run_task(self, update_id: int, chat_id: int, text: str, cancellation: threading.Event):
        session = None
        try:
            def execute(session_id):
                nonlocal session
                try:
                    session = self.application.create_session(resume=session_id)
                except SessionNotFoundError:
                    raise RuntimeError("关联的会话不存在，未自动创建新会话") from None
                result = session.run_request(text, cancellation=cancellation)
                status = "unknown" if cancellation.is_set() or result is None else "completed"
                reply = ("任务已取消，已发生的操作不会撤销。" if cancellation.is_set()
                         else result if result else "本次任务未能完成，请检查会话记录；不会自动重试。")
                return status, reply
            self.state.deliver(update_id, execute, self._safe_send,
                               "任务中断，执行状态可能未知；请核对工作区后再发送新任务。")
        except Exception:
            # A failed durable transition cannot be safely replaced with a claim of completion.
            print("[Telegram] 渠道状态无法保存；请核对会话记录。", file=sys.stderr)
            with self._lock:
                self._closed = True
        finally:
            if session is not None:
                session.close()
            with self._lock:
                self._busy = False
                self._cancel = None
                if not self._closed:
                    self._launch_next_locked()

    def run(self):
        try:
            self.state.notify_pending(
                self._safe_send,
                "上次任务在运行中中断，执行状态未知；未自动重试，请先核对工作区。",
                "上次任务已结束，但回复送达状态未知；请检查会话记录。任务未重跑。",
                "上次取消指令的回执送达状态未知；请核对当前任务。")
            offset = self.state.offset
            while not self._closed:
                try:
                    updates = self.transport.poll(offset)
                except TelegramTransportError:
                    print("[Telegram] 拉取失败，请检查网络或配置。", file=sys.stderr)
                    time.sleep(2)
                    continue
                for update in updates:
                    if not isinstance(update, dict) or type(update.get("update_id")) is not int:
                        raise TelegramTransportError("Telegram getUpdates returned invalid update")
                    update_id = update["update_id"]
                    if self.process_update(update):
                        offset = self.state.offset
                    else:
                        offset = self.state.acknowledge_ignored(update_id)
        finally:
            self.close()

    def close(self):
        with self._lock:
            self._closed = True
            if self._cancel is not None:
                self._cancel.set()
            worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join()  # do not close session/state while an in-flight tool may still commit
        if self._owns_application:
            self.application.close()
        self.state.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Jarvis Telegram private bot (Windows)")
    parser.add_argument("--env-file", type=Path, default=Path.cwd() / ".env")
    args = parser.parse_args(argv)
    if os.name != "nt":
        print("Telegram Bot 目前仅支持 Windows 沙箱环境。", file=sys.stderr)
        return 2
    try:
        values = _read_dotenv(args.env_file)
        token = _setting(values, "TELEGRAM_BOT_TOKEN")
        raw_id = _setting(values, "TELEGRAM_ALLOWED_USER_ID")
        if not token or not raw_id or not raw_id.isdecimal() or int(raw_id) < 1:
            raise ConfigurationError("需配置 TELEGRAM_BOT_TOKEN 和数字 TELEGRAM_ALLOWED_USER_ID")
        config = Config.from_env(args.env_file)
        bot = TelegramBot(config, int(raw_id), TelegramTransport(token), secret=token)
    except (ConfigurationError, ValueError) as exc:
        print(f"Bot 配置错误：{exc}", file=sys.stderr)
        return 2
    try:
        bot.run()
    except KeyboardInterrupt:
        pass
    finally:
        bot.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
