"""``indus sessions`` housekeeping: prune, archive, optimize, repair, recover, import (P07-15).

Built on the P02 store APIs: ``maintenance.prune`` (plans and age prunes), ``deletion.delete_many``
(filtered deletes of exactly the ids that were shown), ``sessions.set_archived``,
``maintenance.vacuum`` / ``holders_of``, ``browse.integrity``, ``fts.rebuild`` and
``portability.import_jsonl``.

Rules every verb follows: the preview and ``--dry-run`` only read (read-only connection); a
destructive step asks first and needs ``-y`` without a terminal; a bulk rewrite (prune, optimize,
repair) never runs while another process or another handle in this process has the store open,
unless ``--force``. ``optimize`` treats that as a skip with the holder list, not a failure
(R03.25). ``repair`` never opens a structurally damaged file for writing (opening would quarantine
it); ``recover`` salvages such a file read-only into a new store.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import click

from indus.cli.backup_rules import copy_database
from indus.cli.cmdkit import age_label, cli_state, err, human_size
from indus.cli.commands.sessions import StoreProblem, read_view, writable_store
from indus.cli.prompts import confirm, io_for
from indus.cli.session_filters import (
    DAY,
    FilterError,
    SessionFilter,
    SessionRow,
    build_filter,
    days_between,
    filter_options,
    select_rows,
)
from indus.foundation.errors import StoreError, StoreUnavailable
from indus.foundation.home import secure_dir
from indus.foundation.scope import Scope
from indus.state import (
    browse,
    connection,
    deletion,
    fts,
    maintenance,
    portability,
    registry,
    sessions,
)
from indus.state.readpool import open_read_only
from indus.state.registry import StoreHandle

__all__ = ["DEFAULT_NEVER_ACTIVE_DAYS", "DEFAULT_PRUNE_DAYS", "SALVAGE_TABLES", "register"]

DEFAULT_PRUNE_DAYS = 90
DEFAULT_NEVER_ACTIVE_DAYS = 30
PREVIEW_ROWS = 20
SALVAGE_CHUNK = 500
# Copied by ``recover``, parents before children. The lease and lock tables are runtime state: a
# partial copy could make a later run believe work is still in flight, so they are left out.
SALVAGE_TABLES = ("system_prompts", "sessions", "messages", "model_usage", "session_meta")


def _now(scope: Scope) -> float:
    return scope.clock.now().timestamp()


def _filter(values: dict[str, Any], scope: Scope) -> SessionFilter:
    try:
        return build_filter(values, scope.clock.now())
    except FilterError as exc:
        raise click.UsageError(str(exc)) from exc


def _preview(verb: str, rows: Sequence[SessionRow], now: float) -> None:
    click.echo(f"{verb} {len(rows)} session(s):")
    for row in rows[:PREVIEW_ROWS]:
        name = row.title or "(untitled)"
        seen = age_label(now - row.last_active) if row.last_active is not None else "—"
        click.echo(f"  {row.id:<24} {name[:40]:<40} {row.message_count:>5} msgs  {seen}")
    if len(rows) > PREVIEW_ROWS:
        click.echo(f"  … and {len(rows) - PREVIEW_ROWS} more")


def _holders_block(found: Sequence[maintenance.Holder]) -> list[str]:
    lines = ["The session store is open elsewhere:"]
    for holder in found[:10]:
        pid = str(holder.pid) if holder.pid >= 0 else "?"
        lines.append(f"  pid {pid}: {holder.what}")
    return lines


@contextlib.contextmanager
def _private_handle(scope: Scope) -> Any:
    """An unshared writable handle, so every shared handle counts as another holder."""
    try:
        handle = registry.open_unshared(scope)
    except StoreError as exc:
        raise StoreProblem(f"The session store cannot be opened: {exc}") from exc
    try:
        yield handle
    finally:
        handle.close()


# -- prune ----------------------------------------------------------------------------------------


def _rows_for(conn: sqlite3.Connection, ids: Sequence[str], now: float) -> list[SessionRow]:
    """Preview rows for ``ids`` in that order (any source)."""
    if not ids:
        return []
    wanted = set(ids)
    everyone = SessionFilter(sources=tuple(_all_sources(conn)))
    found = {r.id: r for r in select_rows(conn, everyone, now=now) if r.id in wanted}
    return [found[i] for i in ids if i in found]


def _all_sources(conn: sqlite3.Connection) -> list[str]:
    return [str(r[0]) for r in conn.execute("SELECT DISTINCT source FROM sessions")]


def _plan(
    conn_view: Any,
    flt: SessionFilter,
    *,
    now: float,
    include_pinned: bool,
    never_active: bool,
) -> tuple[list[str], list[str], bool, float, str | None]:
    """``(ids, spared, simple, days, source)``; ``simple`` = P02's own prune runs the delete."""
    with conn_view.read() as conn:
        if never_active:
            cutoff = (
                flt.older_than
                if flt.older_than is not None
                else now - DEFAULT_NEVER_ACTIVE_DAYS * DAY
            )
            extra = ["s.message_count = 0", "s.title IS NULL", "s.tool_call_count = 0"]
            if not include_pinned:
                extra.append("s.pinned = 0")
            rows = select_rows(conn, replace(flt, older_than=cutoff), now=now, extra_where=extra)
            return [r.id for r in rows], [], False, 0.0, None
        if flt.older_than is not None:
            days = days_between(flt.older_than, now)
        else:
            days = float(DEFAULT_PRUNE_DAYS) if flt.empty else 0.0
        source = flt.sources[0] if len(flt.sources) == 1 else None
        plan = maintenance.prune(conn_view.as_handle(), days, source=source, dry_run=True)
        simple = flt.only_age_and_source and not include_pinned
        if simple:
            return list(plan.ids), list(plan.spared_guarded), True, days, source
        matched = {
            r.id for r in select_rows(conn, replace(flt, older_than=None), now=now, ended_only=True)
        }
        pool = [*plan.ids, *(plan.spared_pinned if include_pinned else [])]
        return [i for i in pool if i in matched], list(plan.spared_guarded), False, days, source


def prune_command(
    include_pinned: bool,
    never_active: bool,
    force: bool,
    dry_run: bool,
    assume_yes: bool,
    **values: Any,
) -> int:
    """Delete old ended sessions (bare: quiet for 90 days); shows the list first."""
    state = cli_state()
    scope = state.scope
    flt = _filter(values, scope)
    now = _now(scope)
    with read_view(scope) as view:
        if view is None:
            click.echo("Nothing to prune: no session store yet.")
            return 0
        ids, guarded, simple, days, source = _plan(
            view, flt, now=now, include_pinned=include_pinned, never_active=never_active
        )
        with view.read() as conn:
            rows = _rows_for(conn, ids, now)
    if not ids:
        click.echo("Nothing to prune.")
        return 0
    _preview("Would delete" if dry_run else "Will delete", rows, now)
    if guarded:
        click.echo(f"In use right now and left alone: {len(guarded)} session(s).")
    if dry_run:
        return 0
    answer = confirm(
        io_for(state, "Nothing was deleted: pass -y to prune without a terminal."),
        f"Delete these {len(ids)} session(s)? This cannot be undone.",
        assume_yes=assume_yes,
    )
    if answer is None:
        return 1
    if not answer:
        click.echo("Nothing was deleted.")
        return 0
    with _private_handle(scope) as handle:
        found = maintenance.holders_of(handle)
        if found and not force:
            err(*_holders_block(found))
            err("Nothing was deleted. Close those, or pass --force to prune anyway.")
            return 1
        if simple:
            report = maintenance.prune(handle, days, source=source)
            deleted, skipped = report.count, len(report.spared_guarded)
        else:
            outcome = deletion.delete_many(handle, ids)
            deleted, skipped = len(outcome.deleted), len(outcome.guarded)
    click.echo(f"Deleted {deleted} session(s).")
    if skipped:
        click.echo(f"Left {skipped} session(s) that were in use.")
    return 0


# -- archive --------------------------------------------------------------------------------------


def archive_command(include_open: bool, dry_run: bool, assume_yes: bool, **values: Any) -> int:
    """Archive (hide from lists, keep) the sessions matching the filters."""
    state = cli_state()
    scope = state.scope
    flt = _filter(values, scope)
    if flt.empty:
        raise click.UsageError(
            "give at least one filter, for example --older-than 30d or --source telegram"
        )
    now = _now(scope)
    with read_view(scope) as view:
        if view is None:
            click.echo("Nothing to archive: no session store yet.")
            return 0
        with view.read() as conn:
            rows = select_rows(
                conn,
                flt,
                now=now,
                ended_only=not include_open,
                extra_where=["s.archived = 0", "s.pinned = 0"],
            )
    if not rows:
        click.echo("Nothing to archive.")
        return 0
    _preview("Would archive" if dry_run else "Will archive", rows, now)
    if dry_run:
        return 0
    answer = confirm(
        io_for(state, "Nothing was archived: pass -y to archive without a terminal."),
        f"Archive these {len(rows)} session(s)?",
        assume_yes=assume_yes,
    )
    if answer is None:
        return 1
    if not answer:
        click.echo("Nothing was archived.")
        return 0
    done = 0
    with writable_store(scope) as store:
        if store is not None:
            done = sum(1 for r in rows if sessions.set_archived(store, r.id, True))
    click.echo(f"Archived {done} session(s).")
    return 0


# -- optimize -------------------------------------------------------------------------------------


def optimize_command(force: bool) -> int:
    """Give unused space back to the disk (VACUUM); skipped while the store is in use."""
    scope = cli_state().scope
    if not connection.default_store_path(scope).is_file():
        click.echo("Nothing to optimise: no session store yet.")
        return 0
    with _private_handle(scope) as handle:
        try:
            report = maintenance.vacuum(handle, force=force)
        except maintenance.VacuumRefused as exc:
            click.echo("Skipped: " + _holders_block(exc.holders)[0])
            for line in _holders_block(exc.holders)[1:]:
                click.echo(line)
            click.echo("Close them and run this again, or pass --force to run anyway.")
            return 0
        except StoreError as exc:
            err(f"Not optimised: {exc}")
            return 1
    click.echo(
        f"Optimised {handle.path}: {human_size(report.size_before)} -> "
        f"{human_size(report.size_after)}."
    )
    return 0


# -- repair ---------------------------------------------------------------------------------------


def _problems(report: browse.IntegrityReport) -> tuple[list[str], bool]:
    """``(lines, structural)``."""
    lines: list[str] = []
    structural = report.structure != ["ok"]
    if structural:
        lines.extend(f"  file: {line}" for line in report.structure[:10])
    if report.latched:
        lines.append(f"  marked damaged earlier: {report.latch_reason or 'unknown reason'}")
        structural = True
    if report.fts is False:
        lines.append("  the search index is damaged")
    if report.fts_stale:
        lines.append("  the search index is out of date")
    return lines, structural


def _check(scope: Scope) -> browse.IntegrityReport:
    try:
        return browse.integrity(scope)
    except StoreUnavailable as exc:
        raise StoreProblem(f"The session store cannot be checked: {exc.message}") from exc
    except sqlite3.Error as exc:
        raise StoreProblem(f"The session store cannot be checked: {exc}") from exc


def _backup_path(scope: Scope) -> Path:
    folder = scope.home / "backups"
    secure_dir(folder, home=scope.home, env=scope.env)
    stamp = scope.clock.now().strftime("%Y%m%d-%H%M%S")
    return folder / f"state-before-repair-{stamp}.db"


def repair_command(check_only: bool, no_backup: bool, force: bool) -> int:
    """Check the session store and rebuild its search index when that is what is wrong."""
    scope = cli_state().scope
    path = connection.default_store_path(scope)
    if not path.is_file():
        click.echo("Nothing to repair: no session store yet.")
        return 0
    lines, structural = _problems(_check(scope))
    if not lines:
        click.echo("The session store is healthy.")
        return 0
    click.echo("Problems found:")
    for line in lines:
        click.echo(line)
    if check_only:
        click.echo("Nothing was changed (--check-only).")
        return 1
    if structural:
        err(
            "The file itself is damaged, so it is not opened for writing. Copy out everything "
            "that can still be read with `indus sessions recover --output FILE`."
        )
        return 1
    with _private_handle(scope) as handle:
        found = maintenance.holders_of(handle)
        if found and not force:
            err(*_holders_block(found))
            err("Nothing was changed. Close those, or pass --force.")
            return 1
        if not no_backup:
            saved = _backup_path(scope)
            copy_database(path, saved)
            click.echo(f"Backup: {saved}")
        if not fts.rebuild(handle):
            err("The search index could not be rebuilt now (the store is busy); try again later.")
            return 1
    lines, _ = _problems(_check(scope))
    if lines:
        err("Still not healthy after the rebuild:", *lines)
        return 1
    click.echo("Rebuilt the search index; the session store is healthy.")
    return 0


# -- recover --------------------------------------------------------------------------------------


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _read_chunks(
    conn: sqlite3.Connection, table: str, columns: Sequence[str]
) -> tuple[list[tuple[Any, ...]], int]:
    """Every readable row of ``table`` (rowid order) and how many chunks could not be read."""
    rows: list[tuple[Any, ...]] = []
    lost = 0
    select = ", ".join(columns)
    try:
        bounds = conn.execute(f"SELECT MIN(rowid), MAX(rowid) FROM {table}").fetchone()
    except sqlite3.DatabaseError:
        bounds = None
    if bounds is not None and bounds[0] is None:
        return rows, lost  # empty table
    last = int(bounds[0]) - 1 if bounds is not None else 0
    top = int(bounds[1]) if bounds is not None else None
    while True:
        try:
            batch = conn.execute(
                f"SELECT rowid, {select} FROM {table} WHERE rowid > ? ORDER BY rowid LIMIT ?",
                (last, SALVAGE_CHUNK),
            ).fetchall()
        except sqlite3.DatabaseError:
            lost += 1
            if top is None or last + SALVAGE_CHUNK >= top:
                break
            last += SALVAGE_CHUNK
            continue
        if not batch:
            break
        rows.extend(tuple(r[1:]) for r in batch)
        last = int(batch[-1][0])
    return rows, lost


def _insert(
    out: StoreHandle, table: str, columns: Sequence[str], rows: list[tuple[Any, ...]]
) -> int:
    if not rows:
        return 0
    marks = ", ".join("?" for _ in columns)
    sql = f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) VALUES ({marks})"

    def run(conn: sqlite3.Connection) -> int:
        before = conn.total_changes
        conn.executemany(sql, rows)
        return conn.total_changes - before

    return out.write(run)


def _salvage_table(
    src: sqlite3.Connection, out: StoreHandle, table: str, keep: Callable[[dict[str, Any]], bool]
) -> tuple[int, int, int]:
    """``(copied, readable, lost chunks)``."""
    try:
        theirs = _columns(src, table)
    except sqlite3.DatabaseError:
        return 0, 0, 1
    with out.read() as conn:
        ours = _columns(conn, table)
    columns = [c for c in ours if c in theirs]
    if not columns:
        return 0, 0, 0
    rows, lost = _read_chunks(src, table, columns)
    kept: list[tuple[Any, ...]] = []
    for row in rows:
        data = dict(zip(columns, row, strict=True))
        if keep(data):
            kept.append(tuple(data[c] for c in columns))
    return _insert(out, table, columns, kept), len(rows), lost


def recover_command(output: str) -> int:
    """Copy every row that can still be read into a new store file."""
    scope = cli_state().scope
    source = connection.default_store_path(scope)
    target = Path(output).expanduser().resolve()
    if not source.is_file():
        err("There is no session store to recover in this profile.")
        return 1
    if target.exists():
        err(f"{target} already exists; name a new file.")
        return 1
    if target == source.resolve():
        err("The output must be a new file, not the store itself.")
        return 1
    try:
        src = open_read_only(source)
    except sqlite3.Error as exc:
        err(f"The store cannot be opened even read-only: {exc}")
        return 1
    copied: dict[str, tuple[int, int, int]] = {}
    try:
        out = registry.open_unshared(scope, target)
        try:
            prompts: set[str] = set()
            ids: set[str] = set()

            def keep_prompt(row: dict[str, Any]) -> bool:
                prompts.add(str(row.get("hash")))
                return True

            def keep_session(row: dict[str, Any]) -> bool:
                if row.get("system_prompt_hash") not in prompts:
                    row["system_prompt_hash"] = None
                row["parent_session_id"] = None
                ids.add(str(row.get("id")))
                return True

            def child_of_kept(row: dict[str, Any]) -> bool:
                return str(row.get("session_id")) in ids

            keepers: dict[str, Callable[[dict[str, Any]], bool]] = {
                "system_prompts": keep_prompt,
                "sessions": keep_session,
                "messages": child_of_kept,
                "model_usage": child_of_kept,
                "session_meta": child_of_kept,
            }
            for table in SALVAGE_TABLES:
                copied[table] = _salvage_table(src, out, table, keepers[table])
            _relink(src, out, ids)
        finally:
            out.close()
    finally:
        src.close()
    click.echo(f"Recovered into {target}:")
    lost_any = False
    for table, (count, readable, lost) in copied.items():
        note = f" ({lost} unreadable part(s) skipped)" if lost else ""
        lost_any = lost_any or bool(lost)
        click.echo(f"  {table:<15} {count} of {readable} readable row(s){note}")
    click.echo(
        "To use it: stop Indus Agent, move the old state.db aside and put this file in its place."
    )
    return 1 if lost_any else 0


def _relink(src: sqlite3.Connection, out: StoreHandle, ids: set[str]) -> None:
    """Restore parent links between recovered sessions, then copy the deletion markers."""
    try:
        links = src.execute(
            "SELECT id, parent_session_id FROM sessions WHERE parent_session_id IS NOT NULL"
        ).fetchall()
    except sqlite3.DatabaseError:
        links = []
    pairs = [(str(p), str(c)) for c, p in links if str(c) in ids and str(p) in ids]
    try:
        stones = src.execute(
            "SELECT key, value FROM meta WHERE key LIKE ?", (sessions.TOMBSTONE_PREFIX + "%",)
        ).fetchall()
    except sqlite3.DatabaseError:
        stones = []

    def run(conn: sqlite3.Connection) -> None:
        conn.executemany("UPDATE sessions SET parent_session_id = ? WHERE id = ?", pairs)
        conn.executemany(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
            [(str(k), v) for k, v in stones],
        )

    out.write(run)


# -- import ---------------------------------------------------------------------------------------


def import_command(file: str) -> int:
    """Import sessions from a JSON-lines export (all of it, or nothing)."""
    scope = cli_state().scope
    path = Path(file).expanduser()
    if not path.is_file():
        err(f"No such file: {path}")
        return 1
    try:
        handle = registry.acquire(scope)
    except StoreError as exc:
        raise StoreProblem(f"The session store cannot be opened: {exc}") from exc
    try:
        report = portability.import_jsonl(handle, path)
    except StoreError as exc:
        err(f"Nothing was imported: {exc}")
        return 1
    finally:
        handle.close()
    if not report.ok:
        err(f"Nothing was imported: {len(report.errors)} problem(s) in {path}:")
        for issue in report.errors[:20]:
            err(f"  {issue}")
        if len(report.errors) > 20:
            err(f"  … and {len(report.errors) - 20} more")
        return 1
    click.echo(f"Imported {len(report.imported)} session(s) from {path}.")
    if report.skipped:
        click.echo(f"Already here, skipped: {len(report.skipped)}.")
    if report.detached:
        click.echo(f"Imported without their missing parent: {len(report.detached)}.")
    if report.retitled:
        click.echo(f"Renamed because the title was taken: {len(report.retitled)}.")
    return 0


# -- registration ---------------------------------------------------------------------------------


def register(group: click.Group) -> None:
    """Add the maintenance verbs to the ``sessions`` group."""
    prune = filter_options(
        older_help="Delete sessions last active before AGE ago (5h, 2d, 1w) or a date; a bare "
        "prune means 90 days, any other filter alone matches every age."
    )(prune_command)
    prune = click.option(
        "--force", is_flag=True, help="Run even while the store is open elsewhere."
    )(prune)
    prune = click.option(
        "--never-active",
        is_flag=True,
        help="Instead delete sessions that were opened and never used, older than AGE "
        "(default 30 days).",
    )(prune)
    prune = click.option("--include-pinned", is_flag=True, help="Delete pinned sessions too.")(
        prune
    )
    group.add_command(click.command("prune")(prune))

    archive = filter_options(older_help="Only sessions last active before AGE ago or a date.")(
        archive_command
    )
    archive = click.option(
        "--include-open", is_flag=True, help="Also archive sessions that were never ended."
    )(archive)
    group.add_command(click.command("archive")(archive))

    optimize = click.option(
        "--force", is_flag=True, help="Run even while the store is open elsewhere."
    )(optimize_command)
    group.add_command(click.command("optimize")(optimize))

    repair = click.option("--check-only", is_flag=True, help="Only report; change nothing.")(
        repair_command
    )
    repair = click.option("--no-backup", is_flag=True, help="Skip the copy taken first.")(repair)
    repair = click.option(
        "--force", is_flag=True, help="Run even while the store is open elsewhere."
    )(repair)
    group.add_command(click.command("repair")(repair))

    recover = click.option(
        "--output", required=True, metavar="FILE", help="The new store file to write."
    )(recover_command)
    group.add_command(click.command("recover")(recover))

    group.add_command(click.command("import")(click.argument("file")(import_command)))
