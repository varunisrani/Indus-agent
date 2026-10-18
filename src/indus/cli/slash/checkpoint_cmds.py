"""Classic CLI handlers for ``/rollback``, ``/checkpoints`` and ``/worktree`` (P11-17, P11-20).

``/rollback`` works on the session's folder (its terminal cwd, else the process cwd):

- ``/rollback`` lists its checkpoints, newest first, numbered from 1; ``--all`` lists every folder;
- ``/rollback N`` restores checkpoint N (after a safety checkpoint, so it can be undone; asks first
  when ``approvals.destructive_slash_confirm`` is on);
- ``/rollback diff N`` shows what changed since checkpoint N;
- ``/rollback N <file>`` restores one file.

``/checkpoints on|off`` switches the snapshot trigger for this session; ``/checkpoints`` (or
``status``) says whether it is on. ``/worktree`` is ``indus.cli.commands.worktree`` (imported on
use, so the command table stays light).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime

from indus.backends.checkpoints import rollback
from indus.backends.checkpoints.rollback import RollbackError
from indus.backends.checkpoints.store import ShadowStore, config_value
from indus.backends.checkpoints.trigger import TRIGGER
from indus.cli.slash.context import SlashContext, SlashResult

__all__ = ["HANDLERS"]

_USAGE = "Usage: /rollback [--all] | /rollback N | /rollback diff N | /rollback N <file>"


def _folder(ctx: SlashContext) -> str:
    found = ctx.session_cwd() if ctx.session_cwd is not None else None
    return found or os.getcwd()


def _when(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M")


def _row(cp: rollback.Checkpoint, label: str = "") -> str:
    files = f"  ({cp.files_changed} file{'s' if cp.files_changed != 1 else ''})"
    where = f"  [{label}]" if label else ""
    return f"  {cp.number:>2}. {cp.short}  {_when(cp.at)}{where}  {cp.reason}{files}"


def _number(word: str) -> int | None:
    return int(word) if word.isdigit() else None


def _list(ctx: SlashContext, store: ShadowStore, folder: str, everything: bool) -> None:
    if everything:
        rows = rollback.list_all(store)
        if not rows:
            ctx.say("No checkpoints have been taken in this profile.")
            return
        ctx.say("Checkpoints of every folder (newest first):")
        ctx.say(*(_row(cp, os.path.basename(where) or where) for where, cp in rows))
        return
    rows_here = rollback.list_checkpoints(store, folder)
    if not rows_here:
        on = TRIGGER.enabled(ctx.scope, ctx.session_id)
        hint = "" if on else " Checkpoints are off; /checkpoints on turns them on."
        ctx.say(f"No checkpoints for {folder}.{hint}")
        return
    ctx.say(f"Checkpoints for {folder} (newest first):")
    ctx.say(*(_row(cp) for cp in rows_here))
    ctx.say(
        "  /rollback N restores · /rollback diff N previews · /rollback N <file> restores one file"
    )


def rollback_cmd(ctx: SlashContext, args: str) -> SlashResult:
    words = args.split()
    folder = _folder(ctx)
    backend = str(config_value(ctx.scope, "terminal.backend", "local") or "local")
    if backend != "local":
        ctx.say(f"Checkpoints are taken only on the local backend (this one is {backend}).")
        return SlashResult()
    store = ShadowStore.for_scope(ctx.scope)
    try:
        if not words or words == ["--all"]:
            _list(ctx, store, folder, everything=bool(words))
        elif words[0].lower() == "diff" and len(words) == 2 and _number(words[1]) is not None:
            text = rollback.diff(store, folder, int(words[1]))
            ctx.say(text.rstrip("\n") if text else "No changes since that checkpoint.")
        elif _number(words[0]) is not None and len(words) == 1:
            _restore(ctx, store, folder, int(words[0]))
        elif _number(words[0]) is not None:
            path = args.strip()[len(words[0]) :].strip()
            done = rollback.restore_file(
                store, folder, int(words[0]), path, session_id=ctx.session_id
            )
            ctx.say(f"Restored {done.file} from checkpoint {words[0]} ({done.restored_to}).")
        else:
            ctx.say(_USAGE)
    except RollbackError as exc:
        ctx.say(str(exc))
    return SlashResult()


def _restore(ctx: SlashContext, store: ShadowStore, folder: str, number: int) -> None:
    asks = config_value(ctx.scope, "approvals.destructive_slash_confirm", False)
    if asks and not ctx.confirm(f"Restore {folder} to checkpoint {number}?"):
        ctx.say("Nothing was restored.")
        return
    done = rollback.restore(store, folder, number, session_id=ctx.session_id)
    ctx.say(f"Restored {folder} to checkpoint {number} ({done.restored_to}).")
    if done.removed:
        shown = ", ".join(done.removed[:5]) + (" …" if len(done.removed) > 5 else "")
        ctx.say(f"  Removed files created since: {shown}")
    if done.not_captured:
        ctx.say(f"  Not restored (nested repositories): {', '.join(done.not_captured)}")
    if done.safety:
        ctx.say("  The state before this rollback is checkpoint 1 now (/rollback 1 undoes it).")


def checkpoints_cmd(ctx: SlashContext, args: str) -> SlashResult:
    word = args.strip().lower()
    if word in ("on", "off"):
        TRIGGER.set_enabled(ctx.scope, ctx.session_id, word == "on")
        state = "on" if word == "on" else "off"
        ctx.say(f"Checkpoints are {state} for this session.")
    elif word in ("", "status"):
        state = "on" if TRIGGER.enabled(ctx.scope, ctx.session_id) else "off"
        ctx.say(f"Checkpoints are {state} for this session (/checkpoints on|off).")
    else:
        ctx.say("Usage: /checkpoints [on|off|status]")
    return SlashResult()


def worktree_cmd(ctx: SlashContext, args: str) -> SlashResult:
    from indus.cli.commands.worktree import worktree_slash

    return worktree_slash(ctx, args)


HANDLERS: dict[str, Callable[[SlashContext, str], SlashResult]] = {
    "rollback": rollback_cmd,
    "checkpoints": checkpoints_cmd,
    "worktree": worktree_cmd,
}
