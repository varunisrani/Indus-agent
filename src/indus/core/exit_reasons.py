"""Why a turn ended: the closed ``ExitReason`` set and the failure details that go with it.

``ExitReason`` itself lives in ``indus.protocol.events`` (the protocol layer sits below core) and
is re-exported here. ``Failure`` is what a failed turn reports beside its reason: a short reason
word, whether trying again may help, and when a quota resets if the provider said so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from indus.protocol.events import ExitReason

__all__ = ["ABNORMAL", "FAILING", "INTERRUPTED", "ExitReason", "Failure"]

# Exits that always come with a ``Failure``.
FAILING: frozenset[ExitReason] = frozenset(
    {
        ExitReason.PROVIDER_FAILED,
        ExitReason.PERSISTENCE_FAILED,
        ExitReason.INVALID_TOOL_STRIKES,
        ExitReason.COMPRESSION_EXHAUSTED,
    }
)

INTERRUPTED: frozenset[ExitReason] = frozenset(
    {ExitReason.INTERRUPTED_BY_USER, ExitReason.INTERRUPTED_BY_SYSTEM}
)

# Exits logged at WARNING: anything but a normal completion or a person's stop.
ABNORMAL: frozenset[ExitReason] = frozenset(ExitReason) - {
    ExitReason.COMPLETED,
    ExitReason.INTERRUPTED_BY_USER,
}


@dataclass(frozen=True)
class Failure:
    """``reason`` is a short machine word (``rate_limit``, ``truncated``, ``persistence`` …)."""

    reason: str
    retryable: bool
    resets_at: datetime | None = None
    detail: str = ""
