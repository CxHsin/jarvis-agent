from app.llm_client import ChatMessage, OpenAICompatibleClient


class AgentService:
    def __init__(self, *, llm_client: OpenAICompatibleClient, system_prompt: str) -> None:
        self._llm_client = llm_client
        self._system_prompt = system_prompt

    def generate_reply(self, user_text: str) -> str:
        normalized_text = user_text.strip()
        if not normalized_text:
            raise ValueError("user_text must not be empty")

        messages = [
            ChatMessage(role="system", content=self._system_prompt),
            ChatMessage(role="user", content=normalized_text),
        ]
        return self._llm_client.chat(messages)
