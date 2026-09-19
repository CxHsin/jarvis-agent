"""Single source of truth for window, reserve, trigger line and send limit.

The model loop asks the budget two questions: when to compress, and whether an
estimated input is still sendable.  Keeping both answers here stops the same
window arithmetic from being written three times with slightly different
rounding.

One reserve serves as both the compression trigger and the send limit; the
output cap shrinks with the estimated input instead of being reserved up front
(ADR 0005).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


OUTPUT_FLOOR_TOKENS = 4096


def estimate_tokens(messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]) -> int:
    """Roughly estimate serialized request tokens; this is not an upper bound."""

    payload = json.dumps(
        {"messages": list(messages), "tools": list(tools)},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    # Four UTF-8 bytes is a deliberately rough cross-provider estimate.
    return max(1, math.ceil(len(payload.encode("utf-8")) / 4))


@dataclass(frozen=True)
class ContextBudget:
    """Window arithmetic for one model configuration."""

    window_tokens: int | None
    output_tokens: int
    model_max_output_tokens: int | None = None
    model_max_input_tokens: int | None = None
    reserve_tokens: int = 16_384
    keep_recent_tokens: int = 0

    @classmethod
    def from_config(cls, config: Any) -> "ContextBudget":
        return cls(
            window_tokens=getattr(config, "context_window_tokens", None),
            output_tokens=int(getattr(config, "max_output_tokens", 0) or 0),
            model_max_output_tokens=getattr(config, "model_max_output_tokens", None),
            model_max_input_tokens=getattr(config, "model_max_input_tokens", None),
            reserve_tokens=int(getattr(config, "context_reserve_tokens", 16_384) or 16_384),
            keep_recent_tokens=int(getattr(config, "context_keep_recent_tokens", 0) or 0),
        )

    @property
    def reserve(self) -> int:
        return self.reserve_tokens

    @property
    def input_limit(self) -> int | None:
        """Largest estimated input that may still be sent."""

        if not self.window_tokens:
            return None
        limit = self.window_tokens - self.reserve
        if self.model_max_input_tokens:
            limit = min(limit, self.model_max_input_tokens)
        return limit

    @property
    def trigger(self) -> int | None:
        """Estimated input above which compression runs.

        Compression starts at the send limit: one number decides both when to
        compress and when to refuse sending (ADR 0005).
        """

        return self.input_limit

    def should_compress(self, estimated_input: int) -> bool:
        trigger = self.trigger
        return trigger is not None and estimated_input > trigger

    def over_budget(self, estimated_input: int) -> bool:
        limit = self.input_limit
        return limit is not None and estimated_input > limit

    def output_limit(self, estimated_input: int | None = None) -> int:
        """Tokens reserved for one completion under the current input estimate."""

        cap = self.output_tokens
        if self.model_max_output_tokens:
            cap = min(cap, self.model_max_output_tokens)
        if not self.window_tokens or estimated_input is None:
            return cap
        remaining = self.window_tokens - estimated_input - OUTPUT_FLOOR_TOKENS
        return max(1, min(cap, remaining))

    def validate(self, label: str = "模型") -> None:
        """Reject configurations that cannot compress back under the trigger."""

        if not self.window_tokens:
            return
        limit = self.input_limit or 0
        if limit <= 0:
            raise ValueError(f"{label} 的输出预留/安全余量过大；请调整 MAX_OUTPUT_TOKENS 和上下文参数。")
        if self.keep_recent_tokens >= limit:
            raise ValueError(f"CONTEXT_KEEP_RECENT_TOKENS 必须小于可用输入预算 ({limit})。")
