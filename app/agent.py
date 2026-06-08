import logging

from app.consolidation import Consolidator
from app.conversation_store import ConversationStore
from app.llm_client import OpenAICompatibleClient
from app.memory_policy import MemoryPolicy
from app.memory_store import MemoryStore
from app.plugins import PluginHost
from app.tools.loop import ToolLoop
from app.turns import PassiveTurnOrchestrator

logger = logging.getLogger(__name__)


class AgentService:
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
        self._conversation_store = conversation_store
        self._orchestrator = PassiveTurnOrchestrator(
            llm_client=llm_client,
            system_prompt=system_prompt,
            conversation_store=conversation_store,
            memory_store=memory_store,
            memory_policy=memory_policy or MemoryPolicy(),
            consolidator=consolidator,
            tool_loop=tool_loop,
            plugin_host=plugin_host,
        )

    def generate_reply(self, *, chat_id: int, user_text: str) -> str:
        normalized_text = user_text.strip()
        if not normalized_text:
            raise ValueError("user_text must not be empty")

        with self._conversation_store.lock_chat(chat_id):
            return self._orchestrator.run(chat_id=chat_id, user_text=normalized_text).reply_text
