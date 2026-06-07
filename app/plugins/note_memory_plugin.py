from app.memory_normalizer import MemoryEntry, normalize_text
from app.plugins.types import MemoryWriteContext, PluginSpec


def _before_memory_write(context: MemoryWriteContext) -> list[MemoryEntry]:
    lowered = context.user_text.casefold()
    if not lowered.startswith("note:"):
        return []
    raw = context.user_text.split(":", 1)[1].strip()
    normalized = normalize_text(raw)
    if not normalized:
        return []
    return [
        MemoryEntry(
            tag="note",
            canonical_key=f"note:{normalized.casefold()}",
            display_text=normalized,
        )
    ]


PLUGIN = PluginSpec(
    plugin_id="note_memory",
    plugin_name="Note Memory",
    before_memory_write=_before_memory_write,
)
