from app.consolidation import Consolidator
from app.conversation_store import ConversationStore
from app.memory_policy import MemoryPolicy
from app.memory_store import ConsolidationState, MemorySnapshot, MemoryStoreError
from app.turns.context import PassiveTurnContext
from app.turns.stages.commit import CommitTurnStage

from tests.test_agent import StubMemoryStore


class ConsolidationFailingMemoryStore(StubMemoryStore):
    def write_recent_context(self, text: str) -> None:
        raise MemoryStoreError("write_recent_context failed")


def test_commit_stage_keeps_memory_writes_when_consolidation_fails() -> None:
    memory_store = ConsolidationFailingMemoryStore(
        MemorySnapshot(
            self_text="",
            memory_text="- [note] existing fact",
            recent_context_text="",
            pending_text="",
            history_text="",
            consolidation_state=ConsolidationState(
                last_processed_history_line=0,
                pending_user_message_count=2,
                last_consolidated_at=None,
            ),
        )
    )
    stage = CommitTurnStage(
        conversation_store=ConversationStore(max_rounds=3),
        memory_store=memory_store,  # type: ignore[arg-type]
        memory_policy=MemoryPolicy(),
        consolidator=Consolidator(),
    )
    context = PassiveTurnContext(
        chat_id=1,
        user_text="Please remember that my name is Alex",
        normalized_user_text="Please remember that my name is Alex",
        memory_snapshot=memory_store.load_snapshot(),
        reply_text="noted",
    )

    result = stage.run(context)

    assert result.reply_text == "noted"
    assert memory_store.memory_writes == [
        "- [note] existing fact\n- [identity] My name is Alex"
    ]
    assert memory_store.history_appends == [
        "User: Please remember that my name is Alex",
        "Assistant: noted",
    ]
    assert memory_store.recent_context_writes == []
    assert memory_store.state_writes == []
