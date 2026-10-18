"""``indus sessions``: list, browse, rename, delete, export, stats, pin / unpin / pinned (P07-14).

Everything that only looks opens the store read-only (``state.browse.open_readonly``: a
``mode=ro`` connection, no schema set-up, no migration, no checkpoint, a missing file is reported
and never created; R03.16). The P02 listing and export functions take a store handle; they get a
small view with the scope and that read-only connection instead (``ReadView``). Changes (rename,
delete, pin) go through the shared writable handle, and only when the store file already exists.

The maintenance verbs (prune, archive, optimize, repair, recover, import) live in
``sessions_maint`` and are added to this group at import.
"""

from __future__ import annotations

import contextlib
import html
import os
import sqlite3
import sys
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import click

from indus.cli.args import ChatArgs, GlobalArgs
from indus.cli.cmdkit import age_label, cli_state, err, group, human_size
from indus.cli.prompts import Cancelled, NonInteractive, ask_text, confirm, io_for
from indus.cli.session_filters import FilterError, build_filter, filter_options, select_rows
from indus.foundation import fsio
from indus.foundation.errors import StoreError, StoreUnavailable
from indus.foundation.redact import Redactor
from indus.foundation.scope import Scope
from indus.state import (
    browse,
    connection,
    deletion,
    listing,
    maintenance,
    portability,
    registry,
    rewind,
    sessions,
    titles,
)
from indus.state.registry import StoreHandle

__all__ = [
    "EXPORT_DIR",
    "ReadView",
    "StoreProblem",
    "read_view",
    "resolve_session",
    "sessions_group",
    "writable_store",
]

EXPORT_DIR = "session-exports"
_NO_STORE = "No sessions yet: nothing has been saved in this profile."
_EXT = {"jsonl": "jsonl", "md": "md", "html": "html"}
_SPEAKER = {"user": "You", "assistant": "Indus Agent", "system": "System"}


class StoreProblem(click.ClickException):
    """The store cannot be read or written as asked; exit 1 with one line."""

    exit_code = 1

    def show(self, file: Any = None) -> None:
        click.echo(self.format_message(), err=True)


@dataclass
class ReadView:
    """What the P02 read helpers need from a handle, over a read-only connection."""

    scope: Scope
    store: browse.ReadonlyStore

    @property
    def path(self) -> Path:
        return self.store.path

    def read(self) -> AbstractContextManager[sqlite3.Connection]:
        return self.store.read()

    def as_handle(self) -> StoreHandle:
        """For P02 functions typed on the writable handle that only read through it."""
        return cast(StoreHandle, self)


def _unavailable_line(exc: StoreUnavailable) -> str:
    hint = f" ({exc.hint})" if exc.hint else ""
    return f"The session store cannot be read: {exc.message}{hint}"


@contextlib.contextmanager
def read_view(scope: Scope) -> Generator[ReadView | None]:
    """A read-only view of the store, or None when there is no store file yet."""
    try:
        store = browse.open_readonly(scope)
    except StoreUnavailable as exc:
        if exc.kind == "missing":
            yield None
            return
        raise StoreProblem(_unavailable_line(exc)) from exc
    except sqlite3.Error as exc:
        raise StoreProblem(f"The session store cannot be read right now: {exc}") from exc
    try:
        yield ReadView(scope, store)
    finally:
        store.close()


@contextlib.contextmanager
def writable_store(scope: Scope) -> Generator[StoreHandle | None]:
    """The shared writable handle, or None when no store file exists (never creates one)."""
    if not connection.default_store_path(scope).is_file():
        yield None
        return
    try:
        handle = registry.acquire(scope)
    except StoreError as exc:
        raise StoreProblem(f"The session store cannot be opened: {exc}") from exc
    try:
        yield handle
    finally:
        handle.close()


def resolve_session(view: ReadView | StoreHandle, text: str) -> str:
    """An exact id, a unique id prefix, or a title; raises ``StoreProblem`` otherwise."""
    handle = view.as_handle() if isinstance(view, ReadView) else view
    try:
        return sessions.resolve_id(handle, text)
    except sessions.AmbiguousSessionId as exc:
        raise StoreProblem(f"{exc.message}; give more of the id.") from exc
    except sessions.SessionNotFound:
        found = titles.resolve_title(handle, text.strip())
        if found is None:
            raise StoreProblem(f"No session found matching '{text}'.") from None
        return found


def _now(scope: Scope) -> float:
    return scope.clock.now().timestamp()


# -- list / browse / pinned ---------------------------------------------------------------------


def _label(row: listing.SessionSummary, width: int = 44) -> str:
    text = row.title or row.preview or "(empty)"
    return text if len(text) <= width else text[: width - 1] + "…"


def _table(rows: Sequence[listing.SessionSummary], now: float, *, numbered: bool = False) -> None:
    head = f"{'ID':<24} {'TITLE / PREVIEW':<44} {'SOURCE':<10} {'MSGS':>5}  LAST ACTIVE"
    click.echo(("     " if numbered else "") + head)
    for i, row in enumerate(rows, 1):
        seen = row.last_active if row.last_active is not None else row.started_at
        pin = "*" if row.pinned else " "
        line = (
            f"{row.id:<24} {_label(row):<44} {row.source:<10} {row.message_count:>5}  "
            f"{age_label(now - seen)}{pin}"
        )
        click.echo((f"{i:>3}. " if numbered else "") + line)


def _workspace_match(row: listing.SessionSummary, needle: str) -> bool:
    wanted = needle.strip().rstrip("/\\")
    for place in (row.git_root, row.cwd):
        if not place:
            continue
        if wanted in place or Path(place).name == wanted:
            return True
    return False


def _summaries(
    view: ReadView, filters: listing.ListFilters, limit: int, workspace: str | None
) -> list[listing.SessionSummary]:
    if not workspace:
        return listing.list_sessions(view.as_handle(), filters, limit=limit)
    found: list[listing.SessionSummary] = []
    offset = 0
    page = max(limit * 4, 100)
    while len(found) < limit:
        batch = listing.list_sessions(view.as_handle(), filters, limit=page, offset=offset)
        found.extend(r for r in batch if _workspace_match(r, workspace))
        if len(batch) < page:
            break
        offset += page
    return found[:limit]


def _listing(
    source: str | None, limit: int, workspace: str | None, *, pinned: bool | None = None
) -> list[listing.SessionSummary] | None:
    scope = cli_state().scope
    filters = listing.ListFilters(
        sources=[source] if source else None,
        pinned=pinned,
        archived=None if pinned else False,
    )
    with read_view(scope) as view:
        if view is None:
            return None
        return _summaries(view, filters, max(limit, 0), workspace)


@group(
    "sessions",
    "Look at, export and tidy your saved conversations.",
    short_help="List, export and tidy saved sessions.",
)
def sessions_group() -> None:
    pass


@sessions_group.command("list")
@click.option("--source", default=None, help="Only sessions from this source (cli, telegram, ...).")
@click.option("--limit", type=int, default=20, show_default=True, help="How many to show.")
@click.option(
    "--workspace",
    metavar="NEEDLE",
    default=None,
    help="Only one workspace: part of its folder path, or the folder's name.",
)
def list_command(source: str | None, limit: int, workspace: str | None) -> int:
    """List recent sessions, newest first."""
    rows = _listing(source, limit, workspace)
    if rows is None:
        click.echo(_NO_STORE)
        return 0
    if not rows:
        click.echo("No sessions match.")
        return 0
    _table(rows, _now(cli_state().scope))
    return 0


@sessions_group.command("browse")
@click.option("--source", default=None, help="Only sessions from this source.")
@click.option("--limit", type=int, default=500, show_default=True, help="How many to offer.")
def browse_command(source: str | None, limit: int) -> int:
    """Pick a session to resume (without a terminal: print the list)."""
    state = cli_state()
    rows = _listing(source, limit, None)
    if rows is None:
        click.echo(_NO_STORE)
        return 0
    if not rows:
        click.echo("No sessions match.")
        return 0
    io = io_for(state)
    interactive = io.tty
    _table(rows, _now(state.scope), numbered=interactive)
    if not interactive:
        return 0
    try:
        answer = ask_text(io, "Resume which session (number, Enter to cancel)")
    except (Cancelled, NonInteractive):
        click.echo()
        return 0
    answer = answer.strip()
    if not answer:
        return 0
    if not answer.isdigit() or not 1 <= int(answer) <= len(rows):
        err(f"'{answer}' is not one of the numbers shown.")
        return 1
    picked = rows[int(answer) - 1]
    target = picked.lineage_ids[-1] if picked.lineage_ids else picked.id
    from indus.cli.chat import run_chat

    args = state.globals.merged(GlobalArgs(resume=target))
    return run_chat(state, args, ChatArgs())


@sessions_group.command("pinned")
@click.option("--limit", type=int, default=50, show_default=True, help="How many to show.")
def pinned_command(limit: int) -> int:
    """List pinned sessions."""
    rows = _listing(None, limit, None, pinned=True)
    if rows is None:
        click.echo(_NO_STORE)
        return 0
    if not rows:
        click.echo("No pinned sessions.")
        return 0
    _table(rows, _now(cli_state().scope))
    return 0


# -- rename / delete / pin ----------------------------------------------------------------------


@sessions_group.command("rename")
@click.argument("session_id")
@click.argument("title", nargs=-1, required=True)
def rename_command(session_id: str, title: tuple[str, ...]) -> int:
    """Give a session a title."""
    scope = cli_state().scope
    text = " ".join(title).strip()
    if not titles.sanitize(text):
        err("The title is empty once control characters are removed.")
        return 1
    with writable_store(scope) as store:
        if store is None:
            raise StoreProblem(f"No session found matching '{session_id}'.")
        sid = resolve_session(store, session_id)
        try:
            done = titles.set_title(store, sid, text, source="user")
        except titles.TitleTaken as exc:
            err(f"{exc.message}. Pick another title.")
            return 1
    if not done:
        err(f"Session {sid} kept its title.")
        return 1
    click.echo(f"Renamed {sid} to '{titles.sanitize(text)}'.")
    return 0


@sessions_group.command("delete")
@click.argument("session_id")
@click.option("-y", "--yes", "assume_yes", is_flag=True, help="Do not ask first.")
def delete_command(session_id: str, assume_yes: bool) -> int:
    """Delete a session, its continuations and the sub-agent runs it started."""
    state = cli_state()
    scope = state.scope
    with read_view(scope) as view:
        if view is None:
            raise StoreProblem(f"No session found matching '{session_id}'.")
        sid = resolve_session(view, session_id)
        with view.read() as conn:
            row = sessions.get_in(conn, sid) or {}
            chain = deletion.chain_ids_in(conn, sid)
            linked = len(chain) - 1 + len(deletion.delegate_descendants_in(conn, chain))
    name = row.get("title") or sid
    extra = f" and {linked} linked session(s)" if linked else ""
    answer = confirm(
        io_for(state, "Not deleted: pass -y to delete without a terminal."),
        f"Delete '{name}' ({row.get('message_count', 0)} messages){extra}? This cannot be undone.",
        assume_yes=assume_yes,
    )
    if answer is None:
        return 1
    if not answer:
        click.echo("Kept.")
        return 0
    with writable_store(scope) as store:
        if store is None:
            raise StoreProblem(f"No session found matching '{session_id}'.")
        try:
            removed = deletion.delete_session(store, sid, whole_chain=True)
        except deletion.SessionGuarded as exc:
            err(f"Not deleted: {exc.message}.")
            return 1
    if not removed:
        err(f"Session {sid} was already gone.")
        return 1
    click.echo(f"Deleted {sid}{extra}.")
    return 0


def _set_pins(ids: Sequence[str], pinned: bool) -> int:
    scope = cli_state().scope
    missing: list[str] = []
    done: list[str] = []
    with writable_store(scope) as store:
        for text in ids:
            if store is None:
                missing.append(text)
                continue
            try:
                sid = resolve_session(store, text)
            except StoreProblem as exc:
                err(exc.format_message())
                missing.append(text)
                continue
            sessions.set_pinned(store, sid, pinned)
            done.append(sid)
    word = "Pinned" if pinned else "Unpinned"
    for sid in done:
        click.echo(f"{word} {sid}.")
    return 1 if missing else 0


@sessions_group.command("pin")
@click.argument("ids", nargs=-1, required=True)
def pin_command(ids: tuple[str, ...]) -> int:
    """Pin sessions: pruning and auto-archive leave them alone."""
    return _set_pins(ids, True)


@sessions_group.command("unpin")
@click.argument("ids", nargs=-1, required=True)
def unpin_command(ids: tuple[str, ...]) -> int:
    """Remove the pin from sessions."""
    return _set_pins(ids, False)


# -- stats ----------------------------------------------------------------------------------------


@sessions_group.command("stats")
def stats_command() -> int:
    """Counts and size of the session store."""
    scope = cli_state().scope
    with read_view(scope) as view:
        if view is None:
            click.echo(_NO_STORE)
            return 0
        snap = maintenance.stats(view.as_handle())
        with view.read() as conn:
            by_source = conn.execute(
                "SELECT source, COUNT(*) AS n FROM sessions GROUP BY source ORDER BY n DESC, source"
            ).fetchall()
            flags = conn.execute(
                "SELECT COALESCE(SUM(pinned), 0), COALESCE(SUM(archived), 0), "
                "MIN(started_at), MAX(started_at) FROM sessions"
            ).fetchone()
        path = view.path
        journal = view.store.journal_mode
    file_size = path.stat().st_size if path.is_file() else None
    click.echo(f"Store:      {path}")
    click.echo(f"Size:       {human_size(snap.size_bytes or file_size)} ({journal} journal)")
    click.echo(f"Sessions:   {snap.rows.get('sessions') or 0}")
    click.echo(f"Messages:   {snap.rows.get('messages') or 0}")
    click.echo(f"Pinned:     {int(flags[0])}   Archived: {int(flags[1])}")
    if snap.freelist_ratio is not None:
        click.echo(f"Free pages: {snap.freelist_ratio:.0%}")
    if by_source:
        click.echo("By source:")
        for row in by_source:
            click.echo(f"  {row['source']:<12} {row['n']}")
    return 0


# -- export ---------------------------------------------------------------------------------------


def _content(value: object) -> str:
    return rewind.content_text(value)


def _call_names(calls: object) -> list[str]:
    names: list[str] = []
    if isinstance(calls, list):
        for call in cast(list[Any], calls):
            if isinstance(call, Mapping):
                fn = cast(Mapping[str, Any], call).get("function")
                if isinstance(fn, Mapping):
                    names.append(str(cast(Mapping[str, Any], fn).get("name") or "tool"))
    return names


def render_markdown(objects: Sequence[Mapping[str, Any]]) -> str:
    head = objects[0]
    sid = str(head.get("id"))
    title = str(head.get("title") or f"Session {sid}")
    parts = [f"# {title}", "", f"Session `{sid}` · source {head.get('source')}", ""]
    for obj in objects[1:]:
        role = str(obj.get("role"))
        text = _content(obj.get("content")).rstrip()
        if role == "tool":
            parts += [f"**{obj.get('tool_name') or 'tool'} output**", "", "```", text, "```", ""]
            continue
        parts += [f"## {_SPEAKER.get(role, role)}", "", text, ""]
        names = _call_names(obj.get("tool_calls"))
        if names:
            parts += [f"_Used: {', '.join(names)}_", ""]
    return "\n".join(parts).rstrip() + "\n"


def render_html(objects: Sequence[Mapping[str, Any]]) -> str:
    head = objects[0]
    sid = html.escape(str(head.get("id")))
    title = html.escape(str(head.get("title") or f"Session {head.get('id')}"))
    body: list[str] = []
    for obj in objects[1:]:
        role = str(obj.get("role"))
        text = html.escape(_content(obj.get("content")))
        if role == "tool":
            label = html.escape(str(obj.get("tool_name") or "tool"))
            body.append(f'<section class="tool"><h3>{label} output</h3><pre>{text}</pre></section>')
            continue
        who = html.escape(_SPEAKER.get(role, role))
        names = _call_names(obj.get("tool_calls"))
        used = f"<p>Used: {html.escape(', '.join(names))}</p>" if names else ""
        body.append(f'<section class="{html.escape(role)}"><h2>{who}</h2><pre>{text}</pre>{used}')
        body.append("</section>")
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{title}</title>\n"
        "<style>body{font-family:sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere}.tool pre{background:#f4f4f4}</style>\n"
        f"</head>\n<body>\n<h1>{title}</h1>\n<p>Session <code>{sid}</code></p>\n"
        + "\n".join(body)
        + "\n</body>\n</html>\n"
    )


def _export_objects(
    view: ReadView, ids: Iterable[str], redactor: Redactor | None
) -> list[list[dict[str, Any]]]:
    out: list[list[dict[str, Any]]] = []
    for sid in ids:
        objects = list(portability.export_session(view.as_handle(), sid))
        if not objects:
            continue
        if redactor is not None:
            objects = [cast(dict[str, Any], redactor.redact_obj(o, force=True)) for o in objects]
        out.append(objects)
    return out


def _stamp(scope: Scope) -> str:
    return scope.clock.now().strftime("%Y%m%d-%H%M%S")


def _targets(
    scope: Scope, fmt: str, output: str | None, ids: Sequence[str]
) -> tuple[Path | None, dict[str, Path]]:
    """``(single file or None for stdout, per-session files for md/html)``."""
    ext = _EXT[fmt]
    if fmt == "jsonl":
        if output == "-":
            return None, {}
        name = f"indus-sessions-{_stamp(scope)}.jsonl"
        path = Path(os.path.expanduser(output)) if output else Path.cwd() / name
        if path.is_dir() or (output or "").endswith(("/", os.sep)):
            path = path / name
        return path, {}
    if output == "-":
        raise click.UsageError("only jsonl exports can go to stdout ('-')")
    if len(ids) == 1:
        name = f"indus-session-{ids[0]}.{ext}"
        path = Path(os.path.expanduser(output)) if output else Path.cwd() / name
        if path.is_dir():
            path = path / name
        return None, {ids[0]: path}
    folder = Path(os.path.expanduser(output)) if output else scope.home / EXPORT_DIR
    return None, {sid: folder / f"{sid}.{ext}" for sid in ids}


@sessions_group.command("export")
@click.argument("output", required=False)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["jsonl", "md", "html"]),
    default="jsonl",
    show_default=True,
    help="jsonl (one file, re-importable), md or html (one file per session).",
)
@click.option("--session-id", default=None, help="Export only this session (id, prefix or title).")
@click.option(
    "--redact/--no-redact",
    default=True,
    show_default=True,
    help="Mask API keys, tokens and other secrets in the export.",
)
@click.option("--force", is_flag=True, help="Overwrite files that already exist.")
@filter_options(older_help="Only sessions last active before AGE ago (5h, 2d, 1w) or a date.")
def export_command(
    output: str | None,
    fmt: str,
    session_id: str | None,
    redact: bool,
    force: bool,
    dry_run: bool,
    assume_yes: bool,
    **values: Any,
) -> int:
    """Export sessions as JSON lines, Markdown or HTML (secrets masked unless --no-redact)."""
    scope = cli_state().scope
    try:
        flt = build_filter(values, scope.clock.now())
    except FilterError as exc:
        raise click.UsageError(str(exc)) from exc
    with read_view(scope) as view:
        if view is None:
            click.echo(_NO_STORE)
            return 0
        if session_id:
            ids = [resolve_session(view, session_id)]
        else:
            with view.read() as conn:
                ids = [r.id for r in select_rows(conn, flt, now=_now(scope))]
        if not ids:
            click.echo("No sessions match.")
            return 0
        if dry_run:
            click.echo(f"Would export {len(ids)} session(s):")
            for sid in ids:
                click.echo(f"  {sid}")
            return 0
        single, files = _targets(scope, fmt, output, ids)
        existing = [p for p in [single, *files.values()] if p is not None and p.exists()]
        if existing and not force:
            err(f"{existing[0]} already exists; pass --force to overwrite. Nothing was written.")
            return 1
        groups = _export_objects(view, ids, Redactor(scope) if redact else None)
    messages = sum(len(g) - 1 for g in groups)
    if fmt == "jsonl":
        text = "".join(portability.dump_line(o) + "\n" for g in groups for o in g)
        if single is None:
            sys.stdout.write(text)
            sys.stdout.flush()
            return 0
        single.parent.mkdir(parents=True, exist_ok=True)
        fsio.atomic_write_text(single, text)
        where = str(single)
    else:
        render = render_markdown if fmt == "md" else render_html
        for objects in groups:
            target = files[str(objects[0]["id"])]
            target.parent.mkdir(parents=True, exist_ok=True)
            fsio.atomic_write_text(target, render(objects))
        paths = list(files.values())
        where = str(paths[0].parent) if len(paths) > 1 else str(paths[0])
    note = "" if redact else " (not redacted: check it before sharing)"
    click.echo(f"Exported {len(groups)} session(s), {messages} message(s) to {where}{note}")
    return 0


def _register_maintenance() -> None:
    from indus.cli.commands import sessions_maint

    sessions_maint.register(sessions_group)


_register_maintenance()
