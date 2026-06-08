from __future__ import annotations

from app.consolidation import Consolidator
from app.conversation_store import ConversationStore
from app.llm_client import OpenAICompatibleClient
from app.memory_policy import MemoryPolicy
from app.memory_store import MemoryStore
from app.plugins import PluginHost
from app.tools.loop import ToolLoop
from app.turns.context import PassiveTurnCommitResult, PassiveTurnContext
from app.turns.stages.build_prompt import BuildPromptStage
from app.turns.stages.commit import CommitTurnStage
from app.turns.stages.load_context import LoadTurnContextStage
from app.turns.stages.post_reply import PostReplyStage
from app.turns.stages.run_reasoning import RunReasoningStage


class PassiveTurnOrchestrator:
    def __init__(
        self,
        *,
        llm_client: OpenAICompatibleClient,
        system_prompt: str,
        conversation_store: ConversationStore,
        memory_store: MemoryStore,
        memory_policy: MemoryPolicy | None = None,
        consolidator: Consolidator | None = None,
        tool_loop: ToolLoop | None = None,
        plugin_host: PluginHost | None = None,
    ) -> None:
        resolved_memory_policy = memory_policy or MemoryPolicy()
        resolved_consolidator = consolidator or Consolidator()
        self._load_context = LoadTurnContextStage(
            conversation_store=conversation_store,
            memory_store=memory_store,
            plugin_host=plugin_host,
        )
        self._build_prompt = BuildPromptStage(
            system_prompt=system_prompt,
            memory_policy=resolved_memory_policy,
        )
        self._run_reasoning = RunReasoningStage(
            llm_client=llm_client,
            tool_loop=tool_loop,
            plugin_host=plugin_host,
        )
        self._post_reply = PostReplyStage(plugin_host=plugin_host)
        self._commit = CommitTurnStage(
            conversation_store=conversation_store,
            memory_store=memory_store,
            memory_policy=resolved_memory_policy,
            consolidator=resolved_consolidator,
            plugin_host=plugin_host,
        )

    def run(self, *, chat_id: int, user_text: str) -> PassiveTurnCommitResult:
        context = PassiveTurnContext(
            chat_id=chat_id,
            user_text=user_text,
            normalized_user_text=user_text,
        )
        context = self._load_context.run(context)
        context = self._build_prompt.run(context)
        context = self._run_reasoning.run(context)
        context = self._post_reply.run(context)
        return self._commit.run(context)
