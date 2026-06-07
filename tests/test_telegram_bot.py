from pathlib import Path

from app.telegram_bot import TelegramBot


class StubAgentService:
    def __init__(self, reply: str = "ok") -> None:
        self.reply = reply
        self.calls: list[tuple[int, str]] = []

    def generate_reply(self, *, chat_id: int, user_text: str) -> str:
        self.calls.append((chat_id, user_text))
        return self.reply


class StubSession:
    def __init__(self) -> None:
        self.post_calls: list[tuple[str, dict, int]] = []

    def post(self, url: str, json: dict, timeout: int):  # noqa: A002
        self.post_calls.append((url, json, timeout))
        return StubResponse({"ok": True, "result": {}})


class StubResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class SinglePassTelegramBot(TelegramBot):
    def __init__(self, *, updates: list[dict], **kwargs) -> None:
        super().__init__(**kwargs)
        self._updates = updates
        self.observed_offsets: list[int | None] = []

    def _get_updates(self, *, offset: int | None) -> list[dict]:
        self.observed_offsets.append(offset)
        updates, self._updates = self._updates, []
        if not updates:
            raise KeyboardInterrupt
        return updates


def test_telegram_bot_loads_persisted_offset(tmp_path: Path) -> None:
    offset_path = tmp_path / "telegram-offset.txt"
    offset_path.write_text("42", encoding="utf-8")
    bot = TelegramBot(
        bot_token="bot-token",
        agent_service=StubAgentService(),  # type: ignore[arg-type]
        poll_timeout_seconds=30,
        request_timeout_seconds=5,
        offset_path=offset_path,
        session=StubSession(),  # type: ignore[arg-type]
    )

    assert bot._load_offset() == 42


def test_telegram_bot_persists_offset_after_processing_update(tmp_path: Path) -> None:
    offset_path = tmp_path / "telegram-offset.txt"
    agent_service = StubAgentService(reply="hello back")
    bot = SinglePassTelegramBot(
        updates=[
            {
                "update_id": 100,
                "message": {"chat": {"id": 7}, "text": "hello"},
            }
        ],
        bot_token="bot-token",
        agent_service=agent_service,  # type: ignore[arg-type]
        poll_timeout_seconds=30,
        request_timeout_seconds=5,
        offset_path=offset_path,
        session=StubSession(),  # type: ignore[arg-type]
    )

    try:
        bot.run_forever()
    except KeyboardInterrupt:
        pass

    assert bot.observed_offsets == [None, 101]
    assert agent_service.calls == [(7, "hello")]
    assert offset_path.read_text(encoding="utf-8") == "101"
