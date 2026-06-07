from app.plugins.types import PluginSpec, TurnContext


def _build_context(context: TurnContext) -> list[str]:
    if "concise" not in context.user_text.casefold():
        return []
    return ["Plugin hint: The user explicitly requested concise output. Keep the answer brief."]


PLUGIN = PluginSpec(
    plugin_id="concise_context",
    plugin_name="Concise Context",
    build_context=_build_context,
)
