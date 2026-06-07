from app.plugins import PluginSpec, ProactiveCandidate, ProactiveContext


def _collect(context: ProactiveContext) -> list[ProactiveCandidate]:
    return [
        ProactiveCandidate(
            candidate_id="cand-1",
            plugin_id="simple_candidate",
            kind="reminder",
            summary="Remember to drink water.",
            priority=10,
            dedupe_key="water-reminder",
            suggested_message="Remember to drink water.",
            evidence=("fixture",),
        )
    ]


PLUGIN = PluginSpec(
    plugin_id="simple_candidate",
    plugin_name="Simple Candidate",
    enabled_by_default=True,
    collect_proactive_candidates=_collect,
)
