from app.plugins.types import PluginSpec
from app.tools.base import ToolSpec


def _register_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="ping",
            description="Return a basic health response.",
            arguments=("target",),
            handler=lambda arguments: {"reply": f"pong:{arguments['target']}"},
        )
    ]


PLUGIN = PluginSpec(
    plugin_id="ping_tool",
    plugin_name="Ping Tool",
    register_tools=_register_tools,
)
