# pyright: strict
"""The LSP stop gate (P24-16, A28): late language-server errors before the answer is final.

An edit's language-server verdict may come after its tool result went back (the write waits only
0.08 s). When such verdicts are still queued for the session as the model answers, or checks of
its edits are still running, the gate waits up to 5 s for them (``EditDiagnostics.settle``) and,
when any new errors came in, sends the agent back with them. The message is a durable row (it is
never stripped, so a later replay sends the same bytes). Delivered notes are gone, so one new error
re-enters the loop once; the gate fires at most ``MAX_LSP_NUDGES`` (3) times per turn.

``standard_gates(scope, host_for=…, pre_verify=…)`` is the registry every agent asks (the loop's
``SessionRuntime.stop_gates``, built by ``core.agent``): this gate first, then verify-on-stop
(``verify.on_stop``, read at each answer; ``verify.max_stop_attempts``, ``verify.phase_timeout``),
then the ``pre_verify`` plugin hook (``verify.max_hook_nudges``) when the host has one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from indus.core.stop_gates import (
    MAX_HOOK_NUDGES,
    MAX_VERIFY_NUDGES,
    HostFor,
    PreVerifyGate,
    PreVerifyHook,
    StopContext,
    StopGates,
    VerifyOnStopGate,
)
from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope
from indus.tools.lsp import diagnostics
from indus.tools.lsp.diagnostics import TURN_END_WAIT_S, EditDiagnostics

__all__ = ["HEADER", "MAX_LSP_NUDGES", "LspStopGate", "standard_gates"]

log = logging.getLogger(__name__)

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


# -- the registry an agent asks -------------------------------------------------------------------


def _setting(scope: Scope | None, key: str) -> object:
    config = scope.config if scope is not None else None
    value: object = None
    if config is not None:
        try:
            value = config.get(key)
        except Exception:
            log.debug("%s unreadable; using the default", key, exc_info=True)
    return default_for(key) if value is None else value


def _count(scope: Scope | None, key: str, fallback: int) -> int:
    value = _setting(scope, key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return fallback


def standard_gates(
    scope: Scope | None,
    *,
    host_for: HostFor,
    pre_verify: PreVerifyHook | None = None,
    checker: Callable[[], EditDiagnostics | None] = diagnostics.active,
) -> StopGates:
    """The LSP gate, verify-on-stop and (with a hook) the ``pre_verify`` gate, in that order."""
    timeout = _setting(scope, "verify.phase_timeout")
    gates = StopGates(
        [
            LspStopGate(checker),
            VerifyOnStopGate(
                host_for,
                enabled=lambda: _setting(scope, "verify.on_stop") is True,
                limit=_count(scope, "verify.max_stop_attempts", MAX_VERIFY_NUDGES),
                phase_timeout=float(timeout)
                if isinstance(timeout, int | float) and not isinstance(timeout, bool)
                else None,
            ),
        ]
    )
    if pre_verify is not None:
        limit = _count(scope, "verify.max_hook_nudges", MAX_HOOK_NUDGES)
        gates.register(PreVerifyGate(pre_verify, limit=limit))
    return gates
