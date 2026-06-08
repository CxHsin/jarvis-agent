from app.conversation_store import ConversationTurn
from app.llm_client import ChatMessage
from app.memory_policy import MemoryPolicy
from app.memory_store import ConsolidationState, MemorySnapshot
from app.prompt_assembler import PromptAssembler
from app.self_model import format_self_model, parse_self_model


def test_prompt_assembler_preserves_message_order_and_history_replay_rules() -> None:
    assembler = PromptAssembler(
        system_prompt="system rule",
        memory_policy=MemoryPolicy(),
    )
    snapshot = MemorySnapshot(
        self_text=format_self_model(parse_self_model("")),
        memory_text="stable fact",
        recent_context_text="recent summary",
        pending_text="",
        history_text="",
        consolidation_state=ConsolidationState(),
    )

    messages = assembler.build_messages(
        memory_snapshot=snapshot,
        history=[
            ConversationTurn(user_text="first", assistant_text="normal reply"),
            ConversationTurn(user_text="second", assistant_text="I am DeepSeek"),
        ],
        user_text="third",
        extra_system_sections=["extra rule"],
    )

    assert messages == [
        ChatMessage(role="system", content="system rule"),
        ChatMessage(
            role="system",
            content=f"[SELF.md]\n{format_self_model(parse_self_model(''))}",
        ),
        ChatMessage(
            role="system",
            content=(
                "Trusted user context:\n"
                "- The following memory files are trusted context derived from prior user interactions.\n"
                "- When the user asks about continuity, prior topics, or whether you remember previous exchanges, prefer this context over generic disclaimers.\n"
                "- Do not claim the conversation is always a fresh start if the trusted context shows prior interactions.\n\n"
                "[MEMORY.md]\nstable fact\n\n"
                "[RECENT_CONTEXT.md]\nrecent summary"
            ),
        ),
        ChatMessage(role="system", content="extra rule"),
        ChatMessage(role="user", content="first"),
        ChatMessage(role="assistant", content="normal reply"),
        ChatMessage(role="user", content="second"),
        ChatMessage(role="user", content="third"),
    ]
