from dataclasses import dataclass, field


_SECTION_ORDER = ("Identity", "Capabilities", "Interaction Style", "Constraints")
_DEFAULT_ITEMS = {
    "Identity": [
        "Name: Jarvis",
        "Role: Personal Telegram agent",
    ],
    "Capabilities": [
        "Can answer user questions with the configured LLM",
        "Can use trusted memory files managed by the application",
        "Can maintain continuity through the app-managed memory layers",
    ],
    "Interaction Style": [
        "Prefer concise, direct responses",
        "Preserve technical precision when discussing implementation details",
    ],
    "Constraints": [
        "Do not deny continuity when trusted memory shows prior interactions",
        "Do not invent capabilities not provided by the application",
        "Do not treat assistant self-descriptions as user facts",
    ],
}


@dataclass(frozen=True)
class SelfModel:
    sections: dict[str, list[str]] = field(default_factory=dict)


def default_self_model() -> SelfModel:
    return SelfModel(sections={name: list(items) for name, items in _DEFAULT_ITEMS.items()})


def parse_self_model(text: str) -> SelfModel:
    if not text.strip():
        return default_self_model()

    sections: dict[str, list[str]] = {}
    current_section: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("## "):
            current_section = line[3:].strip()
            if current_section in _SECTION_ORDER and current_section not in sections:
                sections[current_section] = []
            else:
                current_section = None
            continue
        if current_section is None or not line.startswith("- "):
            continue
        value = line[2:].strip()
        if value:
            sections[current_section].append(value)

    merged = {name: list(_DEFAULT_ITEMS[name]) for name in _SECTION_ORDER}
    for name in _SECTION_ORDER:
        if sections.get(name):
            merged[name] = sections[name]
    return SelfModel(sections=merged)


def format_self_model(model: SelfModel) -> str:
    chunks: list[str] = []
    for name in _SECTION_ORDER:
        chunks.append(f"## {name}")
        for item in model.sections.get(name, _DEFAULT_ITEMS[name]):
            chunks.append(f"- {item}")
        chunks.append("")
    return "\n".join(chunks).rstrip()


def apply_interaction_style_updates(model: SelfModel, updates: list[str]) -> SelfModel:
    if not updates:
        return model

    merged = {name: list(items) for name, items in model.sections.items()}
    existing = {item.casefold() for item in merged["Interaction Style"]}
    for update in updates:
        normalized = update.strip()
        if not normalized or normalized.casefold() in existing:
            continue
        merged["Interaction Style"].append(normalized)
        existing.add(normalized.casefold())
    return SelfModel(sections=merged)
