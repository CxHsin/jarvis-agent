from app.consolidation import Consolidator
from app.memory_store import ConsolidationState


def test_consolidator_does_not_rebuild_before_threshold() -> None:
    consolidator = Consolidator()

    result = consolidator.consolidate(
        history_text="User: one\nAssistant: a\nUser: two",
        previous_recent_context_text="old",
        state=ConsolidationState(),
    )

    assert result.recent_context_text is None
    assert result.state.pending_user_message_count == 2
    assert result.state.last_processed_history_line == 3


def test_consolidator_rebuilds_after_threshold() -> None:
    consolidator = Consolidator()

    result = consolidator.consolidate(
        history_text=(
            "User: one\n"
            "Assistant: a\n"
            "User: two\n"
            "Assistant: b\n"
            "User: three\n"
            "Assistant: c"
        ),
        previous_recent_context_text="old",
        state=ConsolidationState(),
    )

    assert result.recent_context_text is not None
    assert "## Compression" in result.recent_context_text
    assert "## Recent Turns" in result.recent_context_text
    assert "The user recently said: one" in result.recent_context_text
    assert "Assistant: c" in result.recent_context_text
    assert result.state.pending_user_message_count == 0
    assert result.state.last_processed_history_line == 6
    assert result.state.last_consolidated_at is not None


def test_consolidator_compression_uses_only_user_lines() -> None:
    consolidator = Consolidator()

    result = consolidator.consolidate(
        history_text=(
            "User: compare memory designs\n"
            "Assistant: I am Jarvis and I remember everything\n"
            "User: focus on SELF md\n"
            "Assistant: noted\n"
            "User: keep it lightweight"
        ),
        previous_recent_context_text="",
        state=ConsolidationState(),
    )

    assert result.recent_context_text is not None
    compression = result.recent_context_text.split("## Recent Turns", maxsplit=1)[0]
    assert "I am Jarvis and I remember everything" not in compression
    assert "compare memory designs" in compression
    assert "keep it lightweight" in compression
