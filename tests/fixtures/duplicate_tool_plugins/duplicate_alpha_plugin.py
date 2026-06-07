from app.plugins import PluginSpec
from app.tools import ToolSpec


def _register_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="alpha",
            description="duplicate",
            arguments=("path",),
            handler=lambda arguments: arguments,
        )
    ]


PLUGIN = PluginSpec(
    plugin_id="duplicate_alpha",
    plugin_name="Duplicate Alpha",
    enabled_by_default=True,
    register_tools=_register_tools,
)
