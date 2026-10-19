"""``indus memory``: see, edit and approve what the agent remembers (P09-24).

- ``show [memory|user]``: the entries of each store that is on, with its usage;
- ``status``: each store on or off, its usage and file, the approval gate, waiting writes;
- ``edit memory|user``: opens a copy of the store file in ``$VISUAL`` / ``$EDITOR`` (``vi``
  otherwise) and saves it under the store lock, but only when the file did not change while it was
  open (the agent may have written meanwhile). Needs a terminal (exit 2 without one);
- ``pending`` / ``approve <id…|all>`` / ``reject <id…|all>``: staged writes (P09-03), approved in
  creation order, each replaced or removed entry printed in full;
- ``approval on|off``: the ``memory.write_approval`` gate.

Provider plugins (P13) extend this group later.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import click

from indus.cli.cmdkit import cli_state, err, group
from indus.cli.commands._support import scope_with_config
from indus.foundation.config import writer
from indus.foundation.errors import IndusError
from indus.foundation.fsio import atomic_write_text
from indus.foundation.home import secure_dir
from indus.foundation.scope import Scope
from indus.learning import pending
from indus.learning.memory.store import (
    DELIMITER,
    STORE_LABELS,
    TARGETS,
    LockBusy,
    MemoryStore,
    Target,
    coerce_target,
    joined_size,
    parse_entries,
    store_lock,
)
from indus.learning.memory.tool import apply_pending

__all__ = [
    "approve_memory",
    "launch_editor",
    "memory_group",
    "reject_ids",
    "set_approval",
    "status_lines",
]

_TARGET = click.Choice(list(TARGETS))


def _run_editor(command: Sequence[str], path: Path) -> int:
    return subprocess.call([*command, str(path)])


launch_editor: Callable[[Sequence[str], Path], int] = _run_editor
"""Runs the editor on a file and returns its exit code (tests swap in their own)."""


def _store(scope: Scope) -> MemoryStore:
    return MemoryStore.for_scope(scope_with_config(scope))


def _pending(scope: Scope) -> pending.PendingStore:
    return pending.PendingStore(scope.home, scope.clock)


def status_lines(scope: Scope) -> list[str]:
    """The ``status`` text (also used by ``/memory status``)."""
    configured = scope_with_config(scope)
    store = MemoryStore.for_scope(configured)
    lines = ["Memory stores:"]
    for target in TARGETS:
        state = "on" if store.enabled(target) else "off"
        usage = store.usage(target).text
        lines.append(f"  {target:<7} {STORE_LABELS[target]:<14} {state:<4} {usage}")
        lines.append(f"          {store.path(target)}")
    gate = "on" if pending.gate_on(configured, "memory") else "off"
    waiting = _pending(scope).count("memory")
    lines.append(f"Write approval: {gate} (indus memory approval on|off)")
    lines.append(f"Waiting for approval: {waiting}")
    return lines


def approve_memory(scope: Scope, which: str | Sequence[str]) -> list[pending.Approval]:
    store = _store(scope)
    return pending.approve(_pending(scope), "memory", which, lambda r: apply_pending(store, r))


def reject_ids(scope: Scope, subsystem: pending.Subsystem, which: str | Sequence[str]) -> list[str]:
    return pending.reject(_pending(scope), subsystem, which)


def set_approval(scope: Scope, subsystem: pending.Subsystem, on: bool) -> None:
    writer.save(scope, {subsystem: {"write_approval": on}})


def which_of(ids: Sequence[str]) -> str | list[str]:
    """``all`` stays a word; anything else is a list of ids."""
    if len(ids) == 1 and ids[0].strip().lower() == "all":
        return "all"
    return [i.strip() for i in ids if i.strip()]


@group("memory", "See, edit and approve what the agent remembers.")
def memory_group() -> None:
    pass


@memory_group.command("show")
@click.argument("target", type=_TARGET, required=False)
def show_command(target: str | None) -> None:
    """Print the saved entries."""
    store = _store(cli_state().scope)
    picked = coerce_target(target)
    chosen: list[Target] = [picked] if picked is not None else list(TARGETS)
    for name in chosen:
        head = f"{STORE_LABELS[name]} ({store.path(name).name})"
        if not store.enabled(name):
            click.echo(f"{head}: off")
            continue
        entries = store.entries(name)
        click.echo(f"{head}: {store.usage(name).text}")
        if not entries:
            click.echo("  (empty)")
        for index, entry in enumerate(entries, 1):
            first, *rest = entry.splitlines() or [""]
            click.echo(f"  {index}. {first}")
            for line in rest:
                click.echo(f"     {line}")


@memory_group.command("status")
def status_command() -> None:
    """Show which stores are on, how full they are, and the approval gate."""
    for line in status_lines(cli_state().scope):
        click.echo(line)


def _editor(scope: Scope) -> list[str]:
    raw = scope.env.get("VISUAL") or scope.env.get("EDITOR") or "vi"
    return shlex.split(raw) or ["vi"]


@memory_group.command("edit")
@click.argument("target", type=_TARGET)
def edit_command(target: str) -> None:
    """Edit a store file in your editor."""
    state = cli_state()
    if not (state.stdin_isatty() and state.stdout_isatty()):
        err(
            "Editing memory needs a terminal. Without one, edit the file directly:",
            f"  {_store(state.scope).path(coerce_target(target) or 'memory')}",
        )
        raise click.exceptions.Exit(2)
    name: Target = coerce_target(target) or "memory"
    store = _store(state.scope)
    path = store.path(name)
    secure_dir(path.parent, home=state.scope.home)
    try:
        original = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    except (OSError, UnicodeDecodeError) as exc:
        err(f"Could not read {path}: {exc}")
        raise click.exceptions.Exit(1) from None
    draft = path.with_name(f".{path.stem}.edit.md")
    atomic_write_text(draft, original, mode=0o600)
    code = launch_editor(_editor(state.scope), draft)
    if code != 0:
        err(f"The editor exited with {code}; nothing was saved. Your draft is at {draft}.")
        raise click.exceptions.Exit(1)
    edited = draft.read_text(encoding="utf-8")
    entries = parse_entries(edited)
    if entries == parse_entries(original):
        draft.unlink(missing_ok=True)
        click.echo("No changes.")
        return
    try:
        with store_lock(path, store.lock_timeout):
            now = path.read_text(encoding="utf-8-sig") if path.exists() else ""
            if now != original:
                err(
                    f"{path.name} changed while it was open, so your edit was not saved. "
                    f"Your text is kept at {draft}; run the command again to merge it."
                )
                raise click.exceptions.Exit(1)
            atomic_write_text(path, DELIMITER.join(entries))
    except (LockBusy, OSError) as exc:
        err(f"Could not save {path.name}: {exc}. Your text is kept at {draft}.")
        raise click.exceptions.Exit(1) from None
    draft.unlink(missing_ok=True)
    size = joined_size(entries)
    click.echo(
        f"Saved {len(entries)} entries to {path.name} ({size:,}/{store.limit(name):,} chars)."
    )
    if size > store.limit(name):
        err(
            f"{path.name} is over its limit; the agent cannot add to it until it is shortened "
            f"(memory.{'notes' if name == 'memory' else 'profile'}_char_limit)."
        )


@memory_group.command("pending")
def pending_command() -> None:
    """List memory writes waiting for approval."""
    click.echo(pending.describe(_pending(cli_state().scope).list("memory")))


@memory_group.command("approve")
@click.argument("ids", nargs=-1, required=True)
def approve_command(ids: tuple[str, ...]) -> None:
    """Apply waiting writes (ids, or all) in the order they were made."""
    results = approve_memory(cli_state().scope, which_of(ids))
    if not results:
        click.echo("Nothing is waiting for approval.")
        return
    for approval in results:
        click.echo(pending.render_approval(approval, surface="cli"))
    if not any(a.ok for a in results):
        raise click.exceptions.Exit(1)


@memory_group.command("reject")
@click.argument("ids", nargs=-1, required=True)
def reject_command(ids: tuple[str, ...]) -> None:
    """Drop waiting writes (ids, or all) without applying them."""
    removed = reject_ids(cli_state().scope, "memory", which_of(ids))
    if not removed:
        err("No waiting write matched.")
        raise click.exceptions.Exit(1)
    click.echo(f"Rejected {', '.join(removed)}.")


@memory_group.command("approval")
@click.argument("switch", type=click.Choice(["on", "off"]))
def approval_command(switch: str) -> None:
    """Turn the approval gate for memory writes on or off."""
    scope = cli_state().scope
    try:
        set_approval(scope, "memory", switch == "on")
    except IndusError as exc:
        err(f"Not saved: {exc}")
        raise click.exceptions.Exit(1) from None
    if switch == "on":
        click.echo(
            "Memory write approval is on: the CLI asks before each write; other surfaces and "
            "background reviews hold writes for `indus memory approve`."
        )
    else:
        click.echo("Memory write approval is off: memory writes are saved straight away.")
