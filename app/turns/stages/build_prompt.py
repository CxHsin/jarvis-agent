from __future__ import annotations

from app.memory_policy import MemoryPolicy
from app.prompt_assembler import PromptAssembler
from app.turns.context import PassiveTurnContext


class BuildPromptStage:
    def __init__(
        self,
        *,
        system_prompt: str,
        memory_policy: MemoryPolicy,
    ) -> None:
        self._prompt_assembler = PromptAssembler(
            system_prompt=system_prompt,
            memory_policy=memory_policy,
        )

    def run(self, context: PassiveTurnContext) -> PassiveTurnContext:
        context.prompt_messages = self._prompt_assembler.build_messages(
            memory_snapshot=context.memory_snapshot,
            history=context.history,
            user_text=context.normalized_user_text,
            extra_system_sections=context.extra_context_sections,
        )
        return context
