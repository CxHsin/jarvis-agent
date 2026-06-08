from app.conversation_store import ConversationTurn
from app.llm_client import ChatMessage
from app.memory_policy import MemoryPolicy
from app.memory_store import MemorySnapshot


class PromptAssembler:
    def __init__(
        self,
        *,
        system_prompt: str,
        memory_policy: MemoryPolicy,
    ) -> None:
        self._system_prompt = system_prompt
        self._memory_policy = memory_policy

    def build_messages(
        self,
        *,
        memory_snapshot: MemorySnapshot | None,
        history: list[ConversationTurn],
        user_text: str,
        extra_system_sections: list[str] | None = None,
    ) -> list[ChatMessage]:
        messages = [ChatMessage(role="system", content=self._system_prompt)]
        memory_sections = self._memory_policy.build_memory_sections(memory_snapshot)
        messages.extend(ChatMessage(role="system", content=section) for section in memory_sections)
        messages.extend(
            ChatMessage(role="system", content=section)
            for section in (extra_system_sections or [])
            if section.strip()
        )

        for turn in history:
            messages.append(ChatMessage(role="user", content=turn.user_text))
            if self._memory_policy.should_replay_assistant_text(turn.assistant_text):
                messages.append(ChatMessage(role="assistant", content=turn.assistant_text))

        messages.append(ChatMessage(role="user", content=user_text))
        return messages
