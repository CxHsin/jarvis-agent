from pathlib import Path

from jarvis.services.sessions import SessionMessage, SessionStore


def test_session_store_trims_history(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json", history_limit=2)
    store.append("1", SessionMessage(role="user", content="a"))
    store.append("1", SessionMessage(role="assistant", content="b"))
    store.append("1", SessionMessage(role="user", content="c"))
    messages = store.get_messages("1")
    assert [item.content for item in messages] == ["b", "c"]
