from app.plugins import PluginSpec, ProactiveContext


def _bad_candidates(context: ProactiveContext) -> list:
    return ["bad"]  # type: ignore[list-item]


PLUGIN = PluginSpec(
    plugin_id="bad_proactive",
    plugin_name="Bad Proactive",
    enabled_by_default=True,
    collect_proactive_candidates=_bad_candidates,
)
