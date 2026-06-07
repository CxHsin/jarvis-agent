from app.plugins import PluginSpec, TurnContext


def _bad_context(context: TurnContext) -> list[str]:
    return "bad"  # type: ignore[return-value]


PLUGIN = PluginSpec(
    plugin_id="bad_context",
    plugin_name="Bad Context",
    enabled_by_default=True,
    build_context=_bad_context,
)
