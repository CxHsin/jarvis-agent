import re
from dataclasses import dataclass


_TRAILING_PUNCTUATION = ".,!?;:，。！？；："
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class MemoryEntry:
    tag: str
    canonical_key: str
    display_text: str


def normalize_text(text: str) -> str:
    collapsed = _WHITESPACE_RE.sub(" ", text.strip())
    return collapsed.rstrip(_TRAILING_PUNCTUATION).strip()


def classify_user_memory_entry(text: str) -> MemoryEntry | None:
    normalized = normalize_text(text)
    if not normalized:
        return None

    lowered = normalized.casefold()

    for prefix in ("please remember that ", "remember that "):
        if lowered.startswith(prefix):
            fact = normalized[len(prefix) :].strip()
            nested_entry = classify_user_memory_entry(fact)
            if nested_entry is not None:
                return nested_entry
            return MemoryEntry(
                tag="requested_memory",
                canonical_key=f"requested_memory:{_canonicalize_value(fact)}",
                display_text=fact,
            )

    if lowered.startswith("my name is "):
        value = normalized[len("my name is ") :].strip()
        return MemoryEntry(
            tag="identity",
            canonical_key=f"identity:name:{_canonicalize_value(value)}",
            display_text=f"My name is {value}",
        )

    if lowered.startswith("call me "):
        value = normalized[len("call me ") :].strip()
        return MemoryEntry(
            tag="identity",
            canonical_key=f"identity:call_me:{_canonicalize_value(value)}",
            display_text=f"Call me {value}",
        )

    if lowered.startswith("i prefer "):
        value = normalized[len("i prefer ") :].strip()
        return MemoryEntry(
            tag="preference",
            canonical_key=f"preference:{_canonicalize_value(value)}",
            display_text=f"I prefer {value}",
        )

    if lowered.startswith("i like "):
        value = normalized[len("i like ") :].strip()
        return MemoryEntry(
            tag="preference",
            canonical_key=f"preference:like:{_canonicalize_value(value)}",
            display_text=f"I like {value}",
        )

    if lowered.startswith("i usually "):
        value = normalized[len("i usually ") :].strip()
        return MemoryEntry(
            tag="preference",
            canonical_key=f"preference:usually:{_canonicalize_value(value)}",
            display_text=f"I usually {value}",
        )

    return None


def parse_memory_entries(text: str) -> list[MemoryEntry]:
    entries: list[MemoryEntry] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("- "):
            continue
        body = line[2:].strip()
        if not body:
            continue

        if body.startswith("[") and "] " in body:
            closing = body.index("] ")
            tag = body[1:closing].strip()
            display_text = body[closing + 2 :].strip()
            canonical_key = _canonical_key_from_tagged_entry(tag, display_text)
            entries.append(
                MemoryEntry(
                    tag=tag,
                    canonical_key=canonical_key,
                    display_text=display_text,
                )
            )
            continue

        inferred = classify_user_memory_entry(body)
        if inferred is not None:
            entries.append(inferred)
            continue

        normalized = normalize_text(body)
        entries.append(
            MemoryEntry(
                tag="note",
                canonical_key=f"note:{_canonicalize_value(normalized)}",
                display_text=normalized,
            )
        )
    return entries


def format_memory_entries(entries: list[MemoryEntry]) -> str:
    return "\n".join(f"- [{entry.tag}] {entry.display_text}" for entry in entries)


def _canonical_key_from_tagged_entry(tag: str, display_text: str) -> str:
    classified = classify_user_memory_entry(display_text)
    if classified is not None:
        return classified.canonical_key
    return f"{tag}:{_canonicalize_value(display_text)}"


def _canonicalize_value(text: str) -> str:
    return normalize_text(text).casefold()
