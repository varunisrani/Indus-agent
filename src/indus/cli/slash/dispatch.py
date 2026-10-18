"""Slash dispatch for the classic REPL (P06-09).

``dispatch(line, ctx)`` runs one ``/…`` line:

1. The first word is lowercased for matching only (arguments keep their case); a registry alias
   resolves to its canonical command.
2. Any command other than ``/resume`` and ``/sessions`` disarms an armed resume list.
3. Observers (a plugin seam) see the command first; whatever they do or raise, dispatch goes on.
4. A built-in with a CLI handler runs, under its busy policy while a turn is running: ``reject``
   prints a short line, ``interrupt_then_dispatch`` stops the turn and waits for it to settle,
   ``dispatch`` runs at once.
5. Otherwise, in order: the user's quick commands, plugin commands, skill commands. A registry
   command implemented by a later phase, or offered only on another surface, says so. Anything else
   is expanded when it is the unique prefix of a runnable name; several matches are listed; no
   match prints "not a command" with the nearest names.

A handler that raises is logged and reported in one line; the REPL keeps running. When a result
switches sessions, ``ctx.session_id`` is updated before ``dispatch`` returns.
"""

from __future__ import annotations

import difflib
import logging
from collections.abc import Mapping

from indus.cli import quick_commands
from indus.cli.slash import control_cmds, session_cmds
from indus.cli.slash.context import ExtensionHandler, Handler, SlashContext, SlashResult
from indus.foundation.errors import IndusError
from indus.protocol.methods import ProtocolError
from indus.service.commands.registry import REGISTRY, CommandDef, command_word, looks_like_command

__all__ = [
    "HANDLERS",
    "MAX_REDISPATCH",
    "after_turn",
    "dispatch",
    "is_slash",
    "runnable_names",
    "take_resume_pick",
]

log = logging.getLogger(__name__)

MAX_REDISPATCH = 4
"""How many times one line may be re-dispatched (prefix expansion, quick aliases)."""

HANDLERS: dict[str, Handler] = {**session_cmds.HANDLERS, **control_cmds.HANDLERS}
"""Canonical command name → the classic CLI's handler."""

_KEEPS_PICKER = frozenset({"resume", "sessions"})


def is_slash(text: str) -> bool:
    """Is this REPL input a slash command (not a path such as ``/usr/bin/env``)?"""
    return looks_like_command(text)


def runnable_names(ctx: SlashContext) -> dict[str, str]:
    """Every name the CLI can run now, with a short note: built-ins with a handler (and their
    aliases), quick, plugin and skill commands."""
    names: dict[str, str] = {}
    for cmd in REGISTRY.commands("cli"):
        if cmd.name in HANDLERS:
            for key in cmd.keys:
                names[key] = cmd.description if key == cmd.name else f"same as /{cmd.name}"
    for name in quick_commands.names(ctx.scope):
        names.setdefault(name, "quick command")
    for name in ctx.extensions.plugin_commands:
        names.setdefault(name.lower(), "plugin command")
    for name in ctx.extensions.skill_commands:
        names.setdefault(name.lower(), "skill")
    return names


def dispatch(line: str, ctx: SlashContext, *, depth: int = 0) -> SlashResult:
    """Run one slash line (see the module notes)."""
    word, args = command_word(line)
    if not word:
        ctx.say("Type a command after the slash, for example /help.")
        return SlashResult()
    cmd = REGISTRY.resolve(word)
    canonical = cmd.name if cmd is not None else word
    if canonical not in _KEEPS_PICKER:
        ctx.state.resume_choices = None
    if depth == 0:
        _observe(ctx, canonical, args)

    if cmd is not None and cmd.offered_on("cli") and cmd.name in HANDLERS:
        return _run_builtin(cmd, HANDLERS[cmd.name], args, ctx)

    quick = quick_commands.lookup(ctx.scope, word)
    if quick is not None:
        return quick_commands.run_quick(word, quick, args, ctx, depth=depth)
    for table in (ctx.extensions.plugin_commands, ctx.extensions.skill_commands):
        handler = _find(table, word)
        if handler is not None:
            return _run_extension(word, handler, args, ctx)

    if cmd is not None:
        _not_here(cmd, ctx)
        return SlashResult()
    return _expand(word, args, ctx, depth)


def _find(table: Mapping[str, ExtensionHandler], word: str) -> ExtensionHandler | None:
    for name, handler in table.items():
        if name.lower() == word:
            return handler
    return None


def _observe(ctx: SlashContext, command: str, args: str) -> None:
    for observer in list(ctx.extensions.observers):
        try:
            observer(command, args, ctx)
        except Exception:
            log.warning("a slash command observer failed on /%s", command, exc_info=True)


def _run_builtin(cmd: CommandDef, handler: Handler, args: str, ctx: SlashContext) -> SlashResult:
    if ctx.is_busy():
        if cmd.busy_policy == "reject":
            ctx.say(
                f"/{cmd.name} has to wait until the reply finishes (or press Ctrl+C to stop it)."
            )
            return SlashResult()
        if cmd.busy_policy == "interrupt_then_dispatch":
            ctx.port.interrupt(ctx.session_id)
            if not ctx.port.wait_idle(ctx.session_id, ctx.busy_wait_s):
                ctx.say(f"The reply did not stop in time, so /{cmd.name} did not run.")
                return SlashResult()
    try:
        result = handler(ctx, args)
    except (IndusError, ProtocolError) as exc:
        log.info("/%s failed: %s", cmd.name, exc)
        hint = getattr(exc, "hint", None)
        ctx.say(f"/{cmd.name} failed: {exc}" + (f" ({hint})" if hint else ""))
        return SlashResult()
    except Exception as exc:
        log.error("/%s failed", cmd.name, exc_info=True)
        ctx.say(f"/{cmd.name} failed: {type(exc).__name__}: {exc}")
        return SlashResult()
    return _settle(result, ctx)


def _run_extension(
    word: str, handler: ExtensionHandler, args: str, ctx: SlashContext
) -> SlashResult:
    try:
        result = handler(args, ctx)
    except Exception as exc:
        log.error("/%s failed", word, exc_info=True)
        ctx.say(f"/{word} failed: {type(exc).__name__}: {exc}")
        return SlashResult()
    return _settle(result if isinstance(result, SlashResult) else SlashResult(), ctx)


def _settle(result: SlashResult, ctx: SlashContext) -> SlashResult:
    if result.switch_to is not None and result.switch_to != ctx.session_id:
        ctx.session_id = result.switch_to
    return result


def _not_here(cmd: CommandDef, ctx: SlashContext) -> None:
    if not cmd.offered_on("cli"):
        where = " and ".join(sorted(cmd.surfaces))
        ctx.say(f"/{cmd.name} is not available here; it works in: {where}.")
    else:
        ctx.say(f"/{cmd.name} is not built yet; it arrives with {cmd.owner}.")


def _expand(word: str, args: str, ctx: SlashContext, depth: int) -> SlashResult:
    names = runnable_names(ctx)
    match = REGISTRY.prefix_matches(word, names)
    if match.kind == "unique" and depth < MAX_REDISPATCH:
        full = f"/{match.names[0]} {args}".rstrip()
        return dispatch(full, ctx, depth=depth + 1)
    if match.kind == "ambiguous":
        listed = ", ".join(f"/{name}" for name in match.names)
        ctx.say(f"/{word} could be several commands: {listed}.", "Type more of the name.")
        return SlashResult()
    close = difflib.get_close_matches(word, list(names), n=3, cutoff=0.6)
    ctx.say(f"/{word} is not a command.")
    if close:
        ctx.say("Did you mean " + ", ".join(f"/{name}" for name in close) + "? /help lists all.")
    else:
        ctx.say("Type /help to see the commands.")
    return SlashResult()


def take_resume_pick(text: str, ctx: SlashContext) -> SlashResult | None:
    """Plain input while a ``/resume`` list is armed: a bare number picks from it (as
    ``/resume N``); anything else disarms the list and returns None (send it as usual)."""
    choices = ctx.state.resume_choices
    if choices is None:
        return None
    stripped = text.strip()
    if not stripped.isdigit():
        ctx.state.resume_choices = None
        return None
    return dispatch(f"/resume {stripped}", ctx)


def after_turn(ctx: SlashContext) -> str | None:
    """Call when a turn ends: writes changes that waited for the session row, and returns the next
    queued message to send (None when the queue is empty)."""
    session_cmds.flush_pending(ctx)
    if ctx.state.queue:
        return ctx.state.queue.pop(0)
    return None
