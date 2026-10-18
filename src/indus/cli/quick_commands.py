"""Quick commands: the user's own ``/name`` shortcuts from ``quick_commands`` (P06-11).

```yaml
quick_commands:
  gs: {type: exec, command: git status --short}
  m: {type: alias, target: model}
```

- ``exec`` runs ``command`` through the shell (the user wrote it; the model never does) with the
  child environment from ``safety.child_env`` (provider keys removed), at most 30 s on the injected
  clock, then prints the output with secrets masked (R04.28). Arguments typed after the name are
  not passed to the shell.
- ``alias`` re-dispatches ``/<target> <args>``; chains stop after a few hops.

Dispatch reaches quick commands only when no built-in has the name (``slash.dispatch``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, cast

from indus.cli import bang
from indus.cli.slash.context import SlashContext, SlashResult
from indus.foundation.scope import Scope
from indus.safety.approvals.decision import setting
from indus.safety.redaction import redact_output

__all__ = ["EXEC_TIMEOUT_S", "lookup", "names", "run_quick"]

log = logging.getLogger(__name__)

EXEC_TIMEOUT_S = 30.0
CONFIG_KEY = "quick_commands"
MAX_OUTPUT_CHARS = 20_000


def _table(scope: Scope) -> dict[str, Mapping[str, Any]]:
    try:
        raw = setting(scope, CONFIG_KEY)
    except Exception:
        log.warning("quick_commands could not be read", exc_info=True)
        return {}
    if not isinstance(raw, Mapping):
        return {}
    table: dict[str, Mapping[str, Any]] = {}
    for name, spec in cast(Mapping[object, object], raw).items():
        if isinstance(name, str) and name.strip() and isinstance(spec, Mapping):
            table[name.strip().lstrip("/").lower()] = cast(Mapping[str, Any], spec)
    return table


def names(scope: Scope) -> list[str]:
    """The configured quick command names, sorted."""
    return sorted(_table(scope))


def lookup(scope: Scope, name: str) -> Mapping[str, Any] | None:
    """The entry for ``name`` (no slash, any case), or None."""
    return _table(scope).get(name.strip().lstrip("/").lower())


def run_quick(
    name: str,
    spec: Mapping[str, Any],
    args: str,
    ctx: SlashContext,
    *,
    depth: int = 0,
    timeout: float = EXEC_TIMEOUT_S,
) -> SlashResult:
    """Run one quick command (see the module notes)."""
    kind = str(spec.get("type") or "").strip().lower()
    if kind == "alias":
        target = str(spec.get("target") or "").strip().lstrip("/")
        if not target:
            ctx.say(f"Quick command /{name} has no target.")
            return SlashResult()
        from indus.cli.slash.dispatch import MAX_REDISPATCH, dispatch

        if depth >= MAX_REDISPATCH:
            ctx.say(f"Quick command /{name} points round in a circle; stopped.")
            return SlashResult()
        return dispatch(f"/{target} {args}".rstrip(), ctx, depth=depth + 1)
    if kind != "exec":
        ctx.say(f"Quick command /{name} has an unknown type '{kind}' (use exec or alias).")
        return SlashResult()
    command = str(spec.get("command") or "").strip()
    if not command:
        ctx.say(f"Quick command /{name} has no command to run.")
        return SlashResult()
    try:
        from indus.safety.child_env import child_env

        env = child_env(ctx.scope, session_id=ctx.session_id)
    except Exception as exc:
        log.error("no safe environment for quick command /%s", name, exc_info=True)
        ctx.say(
            f"/{name} did not run: a safe environment could not be built ({type(exc).__name__})."
        )
        return SlashResult()
    lines: list[str] = []
    outcome = bang.run_shell(
        command,
        env=env,
        cwd=bang.resolve_cwd(ctx),
        timeout=timeout,
        clock=ctx.time,
        on_line=lines.append,
    )
    if outcome.launch_error is not None:
        ctx.say(f"/{name} could not start: {outcome.launch_error}")
        return SlashResult()
    text = "\n".join(lines).strip()
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + "\n[output cut]"
    if text:
        ctx.say(redact_output(text, force=True, scope=ctx.scope))
    if outcome.timed_out:
        ctx.say(f"/{name} timed out after {timeout:g}s.")
    elif outcome.interrupted:
        ctx.say(f"/{name} was interrupted.")
    elif not text:
        ctx.say(f"/{name} printed nothing." + (f" (exit {outcome.code})" if outcome.code else ""))
    elif outcome.code:
        ctx.say(f"(exit {outcome.code})")
    return SlashResult()
