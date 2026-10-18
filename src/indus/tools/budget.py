"""How much tool output may enter the context: per result and per turn.

Defaults: 100,000 characters per result (``mcp__`` tools 50,000; ``read_file`` unlimited, it pages
itself and spilling it would loop), 200,000 per turn, 1,500-character previews. With a known
context window the budgets shrink with it: per result ``window × 4 × 0.15`` clamped to
8,000–100,000, per turn ``window × 4 × 0.30`` clamped to 16,000–200,000 (4 characters per token).
A cap a tool registered is never allowed above the per-result budget, so a small model's budget
still binds tools that declared a large fixed cap.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

CHARS_PER_TOKEN = 4
RESULT_CHARS = 100_000
TURN_CHARS = 200_000
PREVIEW_CHARS = 1_500
MCP_RESULT_CHARS = 50_000
MIN_RESULT_CHARS = 8_000
MIN_TURN_CHARS = 16_000
RESULT_SHARE = 0.15
TURN_SHARE = 0.30

MCP_PREFIX = "mcp__"
UNLIMITED = math.inf
# Never spilled, whatever the overrides say.
PINNED: Mapping[str, float] = MappingProxyType({"read_file": UNLIMITED})


@dataclass(frozen=True)
class Budget:
    per_result: int = RESULT_CHARS
    per_turn: int = TURN_CHARS
    preview: int = PREVIEW_CHARS
    mcp_per_result: int = MCP_RESULT_CHARS
    overrides: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))

    def threshold(self, tool: str, registered_cap: float | None = None) -> float:
        """Characters one result of ``tool`` may have before it is spilled.

        Order: pinned, explicit override, ``mcp__`` prefix, the tool's registered cap (never above
        ``per_result``; an unlimited cap stays unlimited), then ``per_result``.
        """
        if tool in PINNED:
            return PINNED[tool]
        if tool in self.overrides:
            return self.overrides[tool]
        if tool.startswith(MCP_PREFIX):
            return min(self.mcp_per_result, self.per_result)
        if registered_cap is not None:
            if math.isinf(registered_cap):
                return UNLIMITED
            return min(registered_cap, self.per_result)
        return self.per_result


DEFAULT_BUDGET = Budget()


def _clamp(value: float, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def budget_for_window(
    context_tokens: int | None, *, mcp_per_result: int = MCP_RESULT_CHARS
) -> Budget:
    """The budget for a model with ``context_tokens`` of context (unknown or ≤ 0 → defaults)."""
    if not context_tokens or context_tokens <= 0:
        return Budget(mcp_per_result=mcp_per_result)
    window_chars = context_tokens * CHARS_PER_TOKEN
    return Budget(
        per_result=_clamp(window_chars * RESULT_SHARE, MIN_RESULT_CHARS, RESULT_CHARS),
        per_turn=_clamp(window_chars * TURN_SHARE, MIN_TURN_CHARS, TURN_CHARS),
        mcp_per_result=mcp_per_result,
    )
