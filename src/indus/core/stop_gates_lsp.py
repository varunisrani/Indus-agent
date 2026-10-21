# pyright: strict
"""The LSP stop gate (P24-16, A28): late language-server errors before the answer is final.

An edit's language-server verdict may come after its tool result went back (the write waits only
0.08 s). When such verdicts are still queued for the session as the model answers, or checks of
its edits are still running, the gate waits up to 5 s for them (``EditDiagnostics.settle``) and,
when any new errors came in, sends the agent back with them. The message is a durable row (it is
never stripped, so a later replay sends the same bytes). Delivered notes are gone, so one new error
re-enters the loop once; the gate fires at most ``MAX_LSP_NUDGES`` (3) times per turn.
"""

from __future__ import annotations

from collections.abc import Callable

from indus.core.stop_gates import StopContext
from indus.tools.lsp import diagnostics
from indus.tools.lsp.diagnostics import TURN_END_WAIT_S, EditDiagnostics

__all__ = ["HEADER", "MAX_LSP_NUDGES", "LspStopGate"]

MAX_LSP_NUDGES = 3
HEADER = (
    "[System note: the language server reported problems in files you edited this turn after "
    "those tool results had returned. Fix them if they are real, then give your final answer.]"
)


class LspStopGate:
    name = "lsp"
    durable = True

    def __init__(
        self,
        checker: Callable[[], EditDiagnostics | None] = diagnostics.active,
        *,
        wait: float = TURN_END_WAIT_S,
        limit: int = MAX_LSP_NUDGES,
    ) -> None:
        self._checker = checker
        self.wait = wait
        self.limit = limit

    async def check(self, ctx: StopContext, attempt: int) -> str | None:
        checker = self._checker()
        if checker is None or not ctx.owner:
            return None
        notes = await checker.settle(ctx.owner, timeout=self.wait)
        if not notes:
            return None
        return HEADER + "\n\n" + "\n\n".join(notes)
