"""A scripted auxiliary model for side-task tests (P08-04 summaries; P08-14 adds verdicts).

``FakeAuxModel(*steps, clock=…)`` answers ``summarize(prompt)`` with the next step:

- a ``str`` → that text with ``finish_reason="stop"``;
- a ``SummaryReply`` → as given (``truncated(...)`` and ``empty()`` build the usual ones);
- ``Stall(seconds)`` → waits on the fake clock (the engine's deadline usually fires first);
- an exception instance → raised (``overloaded()``, ``unauthorised()``, ``closed_stream()``).

When the script runs out it answers ``default``. Every prompt is kept in ``prompts``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from indus.core.compression.summarize import SummaryReply
from indus.foundation.errors import ProviderHTTPError, ProviderStreamError
from tests.fakes.clock import FakeClock

DEFAULT_NOTES = "## Goal\nKeep working on the task.\n\n## Next steps\nContinue."


@dataclass(frozen=True)
class Stall:
    seconds: float = 10_000.0


def truncated(text: str = "## Goal\nhalf a summ") -> SummaryReply:
    return SummaryReply(text, "length")


def empty() -> SummaryReply:
    return SummaryReply("", "stop")


def overloaded() -> ProviderHTTPError:
    return ProviderHTTPError("the model is overloaded", status=503)


def unauthorised() -> ProviderHTTPError:
    return ProviderHTTPError("invalid credentials", status=401)


def closed_stream() -> ProviderStreamError:
    return ProviderStreamError("stream closed early", body={"error": {"message": "closed"}})


class FakeAuxModel:
    def __init__(
        self, *steps: Any, clock: FakeClock | None = None, default: str = DEFAULT_NOTES
    ) -> None:
        self.steps: list[Any] = list(steps)
        self.clock = clock
        self.default = default
        self.prompts: list[str] = []

    def script(self, steps: Sequence[Any]) -> None:
        self.steps.extend(steps)

    async def summarize(self, prompt: str) -> SummaryReply:
        self.prompts.append(prompt)
        step = self.steps.pop(0) if self.steps else self.default
        if isinstance(step, Stall):
            if self.clock is None:
                raise TimeoutError("stalled")
            await self.clock.sleep(step.seconds)
            return SummaryReply(self.default)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, SummaryReply):
            return step
        return SummaryReply(str(step))
