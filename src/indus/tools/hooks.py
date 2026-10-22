"""The ``ToolHooks`` seam around every dispatched call.

Three points: ``before`` may block a call or replace its arguments, ``after`` observes the result
(a blocked call's too, with error type ``blocked``), ``transform`` may replace it. The default
``NO_HOOKS`` does nothing.

Failure policy:
- the empty seam (``hooks.empty`` true) is fail-open: if even the no-op raises, the tool still runs;
- any other seam is fail-closed at ``before`` (R07.18): a ``before`` that raises — a missed
  deadline (``TimeoutError``) included — blocks the call. The plugin hooks bus
  (``indus.plugins.hooks.PluginToolHooks``) bounds each ``pre_tool_call`` callback itself and turns
  a hung or failing one into a block; a seam may also declare ``before_deadline_s`` and
  ``run_before`` then blocks the call when ``before`` as a whole does not answer in time;
- ``after`` and ``transform`` failures are logged and the original result is kept.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from indus.tools.calls import ToolCall, ToolContext, ToolResult

log = logging.getLogger(__name__)

# The error type of a call that ``before`` refused. ``dispatch`` still hands that call to ``after``
# (zero duration) so observers see it, and never to ``transform``.
BLOCKED = "blocked"


@dataclass(frozen=True)
class BeforeVerdict:
    """``block`` set: the call does not run and the text is returned as its error.
    ``args`` set: the call runs with these arguments instead."""

    block: str | None = None
    args: Mapping[str, Any] | None = None


class ToolHooks(Protocol):
    @property
    def empty(self) -> bool: ...

    async def before(self, call: ToolCall, ctx: ToolContext) -> BeforeVerdict | None: ...

    async def after(
        self, call: ToolCall, ctx: ToolContext, result: ToolResult, duration_s: float
    ) -> None: ...

    async def transform(
        self, call: ToolCall, ctx: ToolContext, result: ToolResult
    ) -> ToolResult | None: ...


class NoHooks:
    """The empty seam."""

    @property
    def empty(self) -> bool:
        return True

    async def before(self, call: ToolCall, ctx: ToolContext) -> BeforeVerdict | None:
        return None

    async def after(
        self, call: ToolCall, ctx: ToolContext, result: ToolResult, duration_s: float
    ) -> None:
        return None

    async def transform(
        self, call: ToolCall, ctx: ToolContext, result: ToolResult
    ) -> ToolResult | None:
        return None


NO_HOOKS: ToolHooks = NoHooks()


def _backstop(hooks: ToolHooks) -> float | None:
    """The seam's own deadline for ``before`` (``before_deadline_s``), when it declares one."""
    if hooks.empty:
        return None
    value = getattr(hooks, "before_deadline_s", None)
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


async def run_before(hooks: ToolHooks, call: ToolCall, ctx: ToolContext) -> BeforeVerdict:
    deadline = _backstop(hooks)
    try:
        if deadline is None:
            verdict = await hooks.before(call, ctx)
        else:
            async with asyncio.timeout(deadline):
                verdict = await hooks.before(call, ctx)
    except Exception as exc:
        if hooks.empty:
            log.debug("empty tool hook seam raised %s; ignored", type(exc).__name__)
            return BeforeVerdict()
        if isinstance(exc, TimeoutError):
            log.warning("before-hook for %s did not answer in time; the call is blocked", call.name)
            return BeforeVerdict(
                block=f"Tool blocked: a pre-call check did not answer in time "
                f"({type(exc).__name__})"
            )
        log.warning(
            "before-hook for %s raised %s; the call is blocked", call.name, type(exc).__name__
        )
        return BeforeVerdict(block=f"Tool blocked: a pre-call check failed ({type(exc).__name__})")
    return verdict if isinstance(verdict, BeforeVerdict) else BeforeVerdict()


async def run_after(
    hooks: ToolHooks, call: ToolCall, ctx: ToolContext, result: ToolResult, duration_s: float
) -> None:
    try:
        await hooks.after(call, ctx, result, duration_s)
    except Exception as exc:
        log.debug("after-hook for %s raised %s; ignored", call.name, type(exc).__name__)


async def run_transform(
    hooks: ToolHooks, call: ToolCall, ctx: ToolContext, result: ToolResult
) -> ToolResult:
    try:
        replaced = await hooks.transform(call, ctx, result)
    except Exception as exc:
        log.debug("transform-hook for %s raised %s; result kept", call.name, type(exc).__name__)
        return result
    return replaced if isinstance(replaced, ToolResult) else result
