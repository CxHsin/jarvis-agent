"""Single source of truth for window, reserve, trigger line and send limit.

The model loop asks the budget two questions: when to compress, and whether an
estimated input is still sendable.  Keeping both answers here stops the same
window arithmetic from being written three times with slightly different
rounding.

Two shapes exist while the flat reserve is rolled out:

- legacy: the reserve is the output allowance plus a window-proportional error
  margin, and compression runs at the earlier of the threshold line and the
  send limit.
- flat (``reserve_tokens`` set): one reserve serves as both the trigger and the
  send limit, and the output cap shrinks with the estimated input.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


OUTPUT_FLOOR_TOKENS = 4096


@dataclass(frozen=True)
class ContextBudget:
    """Window arithmetic for one model configuration."""

    window_tokens: int | None
    output_tokens: int
    model_max_output_tokens: int | None = None
    model_max_input_tokens: int | None = None
    reserve_tokens: int | None = None
    compression_threshold: float = 0.90
    safety_margin: float = 0.02
    keep_recent_tokens: int = 0

    @classmethod
    def from_config(cls, config: Any) -> "ContextBudget":
        return cls(
            window_tokens=getattr(config, "context_window_tokens", None),
            output_tokens=int(getattr(config, "max_output_tokens", 0) or 0),
            model_max_output_tokens=getattr(config, "model_max_output_tokens", None),
            model_max_input_tokens=getattr(config, "model_max_input_tokens", None),
            reserve_tokens=getattr(config, "context_reserve_tokens", None),
            compression_threshold=float(getattr(config, "context_compression_threshold", 0.90)),
            safety_margin=float(getattr(config, "context_safety_margin", 0.02)),
            keep_recent_tokens=int(getattr(config, "context_keep_recent_tokens", 0) or 0),
        )

    @property
    def flat(self) -> bool:
        """Whether the flat reserve replaced the threshold and safety margin."""

        return self.reserve_tokens is not None

    @property
    def error_allowance(self) -> int:
        if self.flat or not self.window_tokens:
            return 0
        return math.ceil(self.window_tokens * self.safety_margin)

    @property
    def reserve(self) -> int:
        if self.reserve_tokens is not None:
            return int(self.reserve_tokens)
        return self.output_tokens + self.error_allowance

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
    def trigger(self) -> float | None:
        """Estimated input above which compression runs."""

        limit = self.input_limit
        if limit is None:
            return None
        if self.flat:
            return float(limit)
        threshold_line = self.window_tokens * self.compression_threshold - self.reserve
        return min(threshold_line, limit)

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
        if not self.flat or not self.window_tokens or estimated_input is None:
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
        trigger = self.trigger
        if trigger is not None and self.keep_recent_tokens >= trigger:
            raise ValueError("CONTEXT_KEEP_RECENT_TOKENS 过大，压缩后无法降到触发线以下；请调小该值或窗口参数。")
