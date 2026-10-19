"""The ``ToolHooks`` seam's failure policy after P13-17: the empty seam fails open, any other seam
fails closed at ``before`` — on an exception, a missed deadline, or its own backstop (R07.18)."""

from __future__ import annotations

import asyncio
from typing import Any

from indus.foundation.blocking import DeadlineMissed
from indus.tools.calls import ToolCall, ToolContext, ToolResult
from indus.tools.hooks import BeforeVerdict, run_before


class Seam:
    def __init__(self, before: Any, *, empty: bool = False, deadline: Any = None) -> None:
        self._before = before
        self._empty = empty
        self.before_deadline_s = deadline

    @property
    def empty(self) -> bool:
        return self._empty

    async def before(self, call: ToolCall, ctx: ToolContext) -> BeforeVerdict | None:
        return await self._before()

    async def after(
        self, call: ToolCall, ctx: ToolContext, result: ToolResult, duration_s: float
    ) -> None:
        return None

    async def transform(
        self, call: ToolCall, ctx: ToolContext, result: ToolResult
    ) -> ToolResult | None:
        return None


def verdict(seam: Seam) -> BeforeVerdict:
    return asyncio.run(run_before(seam, ToolCall("t"), ToolContext()))


async def raising() -> None:
    raise RuntimeError("broken guard")


async def missed() -> None:
    raise DeadlineMissed("hook did not finish within 30 s")


async def forever() -> None:
    await asyncio.Event().wait()


async def allow() -> None:
    return None


def test_empty_seam_fails_open() -> None:
    assert verdict(Seam(raising, empty=True)).block is None


def test_non_empty_seam_fails_closed_on_raise() -> None:
    assert verdict(Seam(raising)).block == "Tool blocked: a pre-call check failed (RuntimeError)"


def test_missed_hook_deadline_blocks_and_names_it() -> None:
    block = verdict(Seam(missed)).block
    assert block == "Tool blocked: a pre-call check did not answer in time (DeadlineMissed)"


def test_backstop_deadline_blocks_a_before_that_never_answers() -> None:
    block = verdict(Seam(forever, deadline=0.02)).block
    assert block is not None and "did not answer in time" in block


def test_backstop_ignored_for_bad_values_and_empty_seams() -> None:
    assert verdict(Seam(allow, deadline=True)).block is None
    assert verdict(Seam(allow, deadline=-1)).block is None
    assert verdict(Seam(allow, deadline="soon")).block is None
    assert verdict(Seam(allow, deadline=5.0)).block is None
