import logging
from dataclasses import dataclass
from typing import Any

import requests

from app.agent import AgentService
from app.llm_client import LLMClientError

logger = logging.getLogger(__name__)


class TelegramAPIError(RuntimeError):
    """Raised when the Telegram Bot API returns an invalid response."""


@dataclass(frozen=True)
class IncomingMessage:
    update_id: int
    chat_id: int
    text: str


class TelegramBot:
    def __init__(
        self,
        *,
        bot_token: str,
        agent_service: AgentService,
        poll_timeout_seconds: int,
        request_timeout_seconds: int,
        session: requests.Session | None = None,
    ) -> None:
        self._agent_service = agent_service
        self._poll_timeout_seconds = poll_timeout_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._session = session or requests.Session()
        self._base_url = f"https://api.telegram.org/bot{bot_token}"

    def run_forever(self) -> None:
        logger.info("Starting Telegram polling loop")
        offset: int | None = None
        while True:
            try:
                updates = self._get_updates(offset=offset)
            except requests.RequestException:
                logger.exception("Telegram polling failed")
                continue
            except TelegramAPIError:
                logger.exception("Telegram API returned an invalid polling response")
                continue

            for update in updates:
                offset = update["update_id"] + 1
                message = _parse_incoming_message(update)
                if message is None:
                    continue
                self._handle_message(message)

    def _handle_message(self, message: IncomingMessage) -> None:
        logger.info("Received Telegram message", extra={"chat_id": message.chat_id})
        try:
            logger.info("Calling LLM", extra={"chat_id": message.chat_id})
            reply_text = self._agent_service.generate_reply(message.text)
            logger.info("LLM call succeeded", extra={"chat_id": message.chat_id})
        except LLMClientError:
            logger.exception("LLM call failed")
            reply_text = "Model is temporarily unavailable. Please try again later."
        except Exception:
            logger.exception("Unexpected agent error")
            reply_text = "The agent hit an unexpected error. Please try again later."

        try:
            self._send_message(chat_id=message.chat_id, text=reply_text)
            logger.info("Sent Telegram reply", extra={"chat_id": message.chat_id})
        except requests.RequestException:
            logger.exception("Failed to send Telegram reply")
        except TelegramAPIError:
            logger.exception("Telegram API returned an invalid sendMessage response")

    def _get_updates(self, *, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": self._poll_timeout_seconds,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset

        response = self._session.get(
            f"{self._base_url}/getUpdates",
            params=payload,
            timeout=self._poll_timeout_seconds + self._request_timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok") or not isinstance(data.get("result"), list):
            raise TelegramAPIError("Invalid getUpdates response")
        return data["result"]

    def _send_message(self, *, chat_id: int, text: str) -> None:
        response = self._session.post(
            f"{self._base_url}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=self._request_timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise TelegramAPIError("Invalid sendMessage response")


def _parse_incoming_message(update: dict[str, Any]) -> IncomingMessage | None:
    update_id = update.get("update_id")
    message = update.get("message")
    if not isinstance(update_id, int) or not isinstance(message, dict):
        return None

    chat = message.get("chat")
    text = message.get("text")
    if not isinstance(chat, dict) or not isinstance(chat.get("id"), int):
        return None
    if not isinstance(text, str) or not text.strip():
        return None

    return IncomingMessage(update_id=update_id, chat_id=chat["id"], text=text)
