from app.conversation_store import ConversationStore, ConversationTurn


def test_conversation_store_appends_and_trims_rounds() -> None:
    store = ConversationStore(max_rounds=2)

    store.append_turn(1, ConversationTurn(user_text="u1", assistant_text="a1"))
    store.append_turn(1, ConversationTurn(user_text="u2", assistant_text="a2"))
    store.append_turn(1, ConversationTurn(user_text="u3", assistant_text="a3"))

    assert store.get_history(1) == [
        ConversationTurn(user_text="u2", assistant_text="a2"),
        ConversationTurn(user_text="u3", assistant_text="a3"),
    ]


def test_conversation_store_keeps_chats_separate() -> None:
    store = ConversationStore(max_rounds=2)

    store.append_turn(1, ConversationTurn(user_text="u1", assistant_text="a1"))
    store.append_turn(2, ConversationTurn(user_text="u2", assistant_text="a2"))

    assert store.get_history(1) == [ConversationTurn(user_text="u1", assistant_text="a1")]
    assert store.get_history(2) == [ConversationTurn(user_text="u2", assistant_text="a2")]


def test_conversation_store_rejects_invalid_max_rounds() -> None:
    try:
        ConversationStore(max_rounds=0)
    except ValueError as exc:
        assert "greater than zero" in str(exc)
    else:
        raise AssertionError("Expected ValueError")
