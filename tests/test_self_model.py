from app.self_model import (
    apply_interaction_style_updates,
    default_self_model,
    format_self_model,
    parse_self_model,
)


def test_parse_self_model_uses_defaults_for_blank_text() -> None:
    model = parse_self_model("")

    rendered = format_self_model(model)
    assert "## Identity" in rendered
    assert "## Interaction Style" in rendered


def test_parse_self_model_preserves_known_sections() -> None:
    model = parse_self_model(
        "## Identity\n"
        "- Name: Jarvis\n\n"
        "## Interaction Style\n"
        "- Be concise\n"
    )

    assert model.sections["Identity"] == ["Name: Jarvis"]
    assert model.sections["Interaction Style"] == ["Be concise"]
    assert model.sections["Capabilities"]


def test_apply_interaction_style_updates_dedupes_values() -> None:
    model = default_self_model()

    updated = apply_interaction_style_updates(
        model,
        ["Answer in bullets", "Answer in bullets"],
    )

    assert updated.sections["Interaction Style"].count("Answer in bullets") == 1
