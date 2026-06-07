from dataclasses import dataclass
from datetime import datetime, timezone

from app.memory_store import ConsolidationState


@dataclass(frozen=True)
class ConsolidationResult:
    recent_context_text: str | None
    state: ConsolidationState


class Consolidator:
    _THRESHOLD = 3
    _RECENT_TURN_LIMIT = 6
    _COMPRESSION_LIMIT = 4

    def consolidate(
        self,
        *,
        history_text: str,
        previous_recent_context_text: str,
        state: ConsolidationState,
    ) -> ConsolidationResult:
        del previous_recent_context_text

        lines = [line.strip() for line in history_text.splitlines() if line.strip()]
        start_index = min(state.last_processed_history_line, len(lines))
        new_lines = lines[start_index:]
        new_user_count = sum(1 for line in new_lines if line.startswith("User: "))
        pending_user_message_count = state.pending_user_message_count + new_user_count

        if pending_user_message_count < self._THRESHOLD:
            return ConsolidationResult(
                recent_context_text=None,
                state=ConsolidationState(
                    last_processed_history_line=len(lines),
                    pending_user_message_count=pending_user_message_count,
                    last_consolidated_at=state.last_consolidated_at,
                ),
            )

        compression = self._build_compression(lines)
        recent_turns = self._build_recent_turns(lines)
        recent_context_text = self._format_recent_context(
            compression=compression,
            recent_turns=recent_turns,
        )
        return ConsolidationResult(
            recent_context_text=recent_context_text,
            state=ConsolidationState(
                last_processed_history_line=len(lines),
                pending_user_message_count=0,
                last_consolidated_at=datetime.now(timezone.utc).isoformat(),
            ),
        )

    def _build_compression(self, lines: list[str]) -> list[str]:
        seen: set[str] = set()
        items: list[str] = []
        for line in reversed(lines):
            if not line.startswith("User: "):
                continue
            content = line[len("User: ") :].strip()
            lowered = content.casefold()
            if lowered in seen:
                continue
            seen.add(lowered)
            items.append(content)
            if len(items) >= self._COMPRESSION_LIMIT:
                break
        items.reverse()
        return [f"The user recently said: {item}" for item in items]

    def _build_recent_turns(self, lines: list[str]) -> list[str]:
        return lines[-self._RECENT_TURN_LIMIT :]

    def _format_recent_context(
        self,
        *,
        compression: list[str],
        recent_turns: list[str],
    ) -> str:
        chunks = ["## Compression"]
        if compression:
            chunks.extend(f"- {item}" for item in compression)
        else:
            chunks.append("- No consolidated recent context yet")
        chunks.append("")
        chunks.append("## Recent Turns")
        if recent_turns:
            chunks.extend(recent_turns)
        else:
            chunks.append("No recent turns")
        return "\n".join(chunks)
