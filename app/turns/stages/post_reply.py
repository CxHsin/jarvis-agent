from __future__ import annotations

from app.plugins import PluginHost
from app.plugins.types import ModelCallResult
from app.turns.context import PassiveTurnContext


class PostReplyStage:
    def __init__(self, *, plugin_host: PluginHost | None = None) -> None:
        self._plugin_host = plugin_host

    def run(self, context: PassiveTurnContext) -> PassiveTurnContext:
        context.turn_notes = (
            self._plugin_host.after_model_call(
                ModelCallResult(
                    chat_id=context.chat_id,
                    user_text=context.normalized_user_text,
                    reply_text=context.reply_text,
                    memory_snapshot=context.memory_snapshot,
                )
            )
            if self._plugin_host is not None
            else []
        )
        return context
