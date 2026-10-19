"""``indus chat`` (also bare ``indus``): pick the interface and the session, then run it.

Interface (R04.19, ``tui_launch.wants_tui``): ``--cli`` → classic; ``--tui`` → the full-screen
renderer; stdin or stdout not a terminal → classic, whatever the settings say; ``INDUS_TUI=1`` or
``display.interface: tui`` → the renderer. When the renderer cannot start (``tui_launch.run``
returns None after saying why) the classic prompt runs.

Then: ``--in DIR`` is entered, ``-w [NAME]`` makes a new worktree of the repository here and enters
it (P11-20; the launch record and ``worktrees.json`` name it, so a resume moves back into it),
``--checkpoints`` turns file checkpoints on for this run, the session is selected
(``session_select``), a resumed session's launch record is checked against the flags given now (a
different value exits 2) and its folder and switches are restored (``resume``), the route is
resolved, and one of three paths runs:

- finite query (``-q``/``--query-file`` with ``--oneshot``, ``-Q`` or no terminal): one turn,
  the reply on stdout, exit codes of ``oneshot.finite_exit_code``;
- seeded REPL (``-q`` on a terminal): the REPL opens and submits the text literally first;
- the REPL.

The REPL runs on an ``InProcessHost``; after it returns, the sessions are closed, the terminal
runtime is shut down on the host's loop and the store handle is released.
"""

from __future__ import annotations

import importlib
import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import click

from indus.cli.app import globals_from, with_global_options
from indus.cli.args import AUTO_WORKTREE, ChatArgs, CliState, GlobalArgs
from indus.cli.handoff import REPL_ENTRY, ReplEntry, ReplLaunch

__all__ = [
    "chat_command",
    "choose_interface",
    "load_repl",
    "read_query",
    "run_chat",
]

log = logging.getLogger(__name__)


def _err(*lines: str) -> None:
    for line in lines:
        click.echo(line, err=True)


def load_repl() -> ReplEntry:
    """The classic REPL's entry (``handoff.REPL_ENTRY``)."""
    module_name, _, attr = REPL_ENTRY.partition(":")
    entry: ReplEntry = getattr(importlib.import_module(module_name), attr)
    return entry


def choose_interface(state: CliState, args: GlobalArgs) -> tuple[str, str | None]:
    """``("classic", None)`` or ``("tui", None)`` (see the module notes)."""
    from indus.cli.tui_launch import wants_tui

    terminal = state.stdin_isatty() and state.stdout_isatty()
    wanted = wants_tui(args, terminal=terminal, env=state.scope.env, config=state.scope.config)
    return ("tui" if wanted else "classic"), None


def read_query(chat: ChatArgs) -> str | None:
    """The query text: ``-q`` as typed, or the file's text (``-`` reads stdin), unchanged."""
    if chat.query_file is None:
        return chat.query
    if chat.query_file == "-":
        return sys.stdin.read()
    return Path(chat.query_file).expanduser().read_text(encoding="utf-8")


@click.command(
    "chat",
    help="Chat with the agent (the default when `indus` is run with no command).",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
)
@click.option(
    "-q",
    "--query",
    default=None,
    help="A first message. On a terminal it opens the chat with this message sent as typed; "
    "with --oneshot, -Q or without a terminal the answer is printed and the command exits.",
)
@click.option(
    "--query-file",
    metavar="PATH",
    default=None,
    help="Read the message from PATH ('-' reads stdin); nothing in it is interpreted.",
)
@click.option(
    "--oneshot",
    "oneshot_exit",
    is_flag=True,
    default=False,
    help="With -q: answer and exit instead of opening the chat.",
)
@click.option("-Q", "--quiet", is_flag=True, default=False, help="Print only the reply.")
@click.option("--max-turns", type=int, default=None, metavar="N", help="Tool-call cap per turn.")
@click.option(
    "--source",
    default=None,
    help="Tag stored with the session (default: cli; 'tool' hides it from session lists).",
)
@click.option(
    "--voice",
    is_flag=True,
    default=False,
    help="Start with voice mode on: hold the record key (Ctrl+Space) to talk.",
)
@click.option(
    "--save-trajectories",
    is_flag=True,
    default=False,
    help="After each turn, append the conversation as a training sample to "
    "trajectory_samples.jsonl (or failed_trajectories.jsonl) in the working directory.",
)
@with_global_options(repeated=True)
@click.pass_context
def chat_command(
    ctx: click.Context,
    query: str | None,
    query_file: str | None,
    oneshot_exit: bool,
    quiet: bool,
    max_turns: int | None,
    source: str | None,
    voice: bool,
    save_trajectories: bool,
    **values: Any,
) -> int:
    state = ctx.find_object(CliState)
    if state is None:
        raise click.UsageError("chat was started without its process state")
    args = state.globals.merged(globals_from(values))
    chat = ChatArgs(
        query=query,
        query_file=query_file,
        oneshot=oneshot_exit,
        quiet=quiet,
        max_turns=max_turns,
        source=source,
        voice=voice,
        save_trajectories=save_trajectories,
    )
    return run_chat(state, args, chat)


def _usage_problem(args: GlobalArgs, chat: ChatArgs) -> str | None:
    from indus.cli.runtime import reasoning_problem

    if chat.query is not None and chat.query_file is not None:
        return "use either -q/--query or --query-file, not both"
    if chat.max_turns is not None and chat.max_turns < 0:
        return "--max-turns must be 0 or more"
    return reasoning_problem(args.reasoning)


def _guards(state: CliState, args: GlobalArgs) -> int | None:
    """P07-03: the first-run guard (skipped when a driver injects the route) and the explicit
    route guard; an int is the exit code."""
    from indus.cli import first_run

    if state.overrides is None or state.overrides.route is None:
        stop = first_run.first_run_guard(state)
        if stop is not None:
            return stop
    return first_run.route_guard(state, args)


def run_chat(state: CliState, args: GlobalArgs, chat: ChatArgs) -> int:
    """Run ``chat`` with the merged global flags; returns the exit code."""
    problem = _usage_problem(args, chat)
    if problem is not None:
        _err(f"indus chat: {problem}")
        return 2
    interface, note = choose_interface(state, args)
    stop = _guards(state, args)
    if stop is not None:
        return stop
    try:
        query = read_query(chat)
    except OSError as exc:
        _err(f"indus chat: cannot read {chat.query_file}: {exc.strerror or exc}")
        return 1
    if interface == "tui":
        from indus.cli import tui_launch

        code = tui_launch.run(state, args, chat, query=query, dev=args.dev)
        if code is not None:
            return code
    terminal = state.stdin_isatty() and state.stdout_isatty()
    finite = query is not None and (chat.oneshot or chat.quiet or not terminal)
    return _ChatRun(state, args, chat, query=query, finite=finite, note=note).run()


class _ChatRun:
    """One chat invocation from session selection to shutdown."""

    def __init__(
        self,
        state: CliState,
        args: GlobalArgs,
        chat: ChatArgs,
        *,
        query: str | None,
        finite: bool,
        note: str | None,
    ) -> None:
        self.state = state
        self.args = args
        self.chat = chat
        self.query = query
        self.finite = finite
        self.notices: list[str] = [note] if note else []
        self.worktree: Any = None

    def run(self) -> int:
        from indus.backends.checkpoints.trigger import TRIGGER
        from indus.cli.session_select import SelectionError, enter_in_dir
        from indus.state import registry

        if self.args.in_dir is not None:
            try:
                enter_in_dir(self.args.in_dir)
            except SelectionError as exc:
                _err(*exc.lines())
                return exc.code
        if self.args.worktree is not None:
            stop = self._enter_worktree()
            if stop is not None:
                return stop
        try:
            store = registry.acquire(self.state.scope)
        except Exception as exc:
            _err(f"indus: the session store could not be opened: {exc}")
            return 1
        if self.args.checkpoints:
            TRIGGER.set_default(True)
        try:
            return self._with_store(store)
        finally:
            if self.args.checkpoints:
                TRIGGER.set_default(None)
            registry.release(store)

    def _enter_worktree(self) -> int | None:
        """``-w``: a new worktree of the repository here, entered (an exit code on failure)."""
        from indus.backends import worktrees
        from indus.cli.session_select import SelectionError, enter_in_dir

        if self.args.resume is not None or self.args.continue_ is not None:
            _err(
                "indus: -w starts a new session in a new worktree; a resumed session keeps its "
                "own folder. Drop -w, or drop -c/-r."
            )
            return 2
        raw = self.args.worktree
        name = None if raw in (None, AUTO_WORKTREE) else raw
        try:
            info = worktrees.create(self.state.scope, Path.cwd(), name)
            enter_in_dir(info.path)
        except worktrees.WorktreeError as exc:
            _err(f"indus: {exc}", "Run `indus -w` from inside a git repository.")
            return 1
        except SelectionError as exc:
            _err(*exc.lines())
            return exc.code
        self.worktree = info
        self.notices.append(f"Worktree: {info.path} (branch {info.branch}, from {info.base})")
        return None

    def _with_store(self, store: Any) -> int:
        from indus.cli import resume, session_select
        from indus.cli.runtime import (
            SOURCE_CLI,
            ServiceSpec,
            learning_startup,
            resolve_route,
            scope_for,
        )
        from indus.foundation.errors import IndusError
        from indus.state import sessions

        scope = self.state.scope
        args = self.args
        cwd = Path.cwd()
        latest_form = (args.resume or "").strip().lower() == session_select.LATEST or (
            args.resume is None and args.continue_ == session_select.BARE_CONTINUE
        )
        crumb = resume.read_breadcrumb(scope) if latest_form else None
        try:
            session_id = session_select.select_session(store, args, cwd=cwd, breadcrumb=crumb)
        except session_select.SelectionError as exc:
            _err(*exc.lines())
            return exc.code

        policy, given = session_select.launch_from_args(args, self.chat, cwd=cwd)
        if self.worktree is not None:
            policy = replace(policy, worktree=self.worktree.path)
        row = sessions.get(store, session_id) if session_id is not None else None
        if session_id is not None:
            stored = resume.apply_switches(session_select.stored_launch(row), row)
            if stored is not None:
                conflicts = session_select.check_frozen(stored, given)
                if conflicts:
                    _err(
                        "indus: this session was started with different settings for "
                        f"{', '.join(conflicts)}; a resumed session keeps its own.",
                        "Drop those flags, or start a new session to change them.",
                    )
                    return 2
            restored = resume.resume_session(
                scope,
                session_id,
                store=store,
                restore=not (args.no_restore_cwd or args.in_dir is not None),
                breadcrumb=False,
            )
            self.notices.extend(restored.notices)
            policy = restored.policy or session_select.replace_cwd(policy, Path.cwd())

        run_scope = scope_for(scope, policy)
        learning_startup(run_scope, store, interactive=not self.finite)
        try:
            route = resolve_route(run_scope, policy, self.state.overrides)
        except IndusError as exc:
            _err(f"indus: {exc.message}", *([exc.hint] if exc.hint else []))
            return 1
        spec = ServiceSpec(
            scope=run_scope,
            route=route,
            policy=policy,
            store=store,
            source=self.chat.source or SOURCE_CLI,
            run_context="single_query" if self.finite else "interactive",
            overrides=self.state.overrides,
            new_session=session_id is None,
            accept_hooks=args.accept_hooks,
            save_trajectories=self.chat.save_trajectories,
        )
        if self.finite:
            from indus.cli.oneshot import run_finite

            _err(*self.notices)

            assert self.query is not None
            return run_finite(spec, self.query, session_id, quiet=self.chat.quiet)
        return self._interactive(spec, session_id, row)

    def _interactive(self, spec: Any, session_id: str | None, row: Any) -> int:
        from indus.cli import resume
        from indus.cli.runtime import make_host, shutdown_terminal
        from indus.protocol.methods import ProtocolError, SessionCreateParams

        try:
            entry = load_repl()
        except (ImportError, AttributeError) as exc:
            _err(f"indus: the chat prompt is not available in this build ({exc}).")
            return 1
        host = make_host(spec)
        try:
            host.start()
        except Exception as exc:
            _err(f"indus: the agent could not start: {exc}")
            return 1
        try:
            cwd = Path(spec.policy.cwd) if spec.policy.cwd else Path.cwd()
            try:
                if session_id is None:
                    created = host.create(SessionCreateParams(cwd=str(cwd)))
                    session_id = created.session_id
                    history: list[Any] = []
                else:
                    host.resume(session_id)
                    history = list(host.call(host.service.store.load(session_id)))
            except ProtocolError as exc:
                _err(f"indus: {exc.message}")
                return 1
            resume.write_breadcrumb(spec.scope, session_id, cwd)
            notices = (*self.notices, *_mcp_notices(spec.scope))
            if self.worktree is not None:
                from indus.backends import worktrees

                worktrees.bind_session(spec.scope, self.worktree.path, session_id)
            launch = ReplLaunch(
                scope=spec.scope,
                host=host,
                session_id=session_id,
                policy=spec.policy,
                model=spec.route.model,
                provider=spec.route.provider,
                cwd=cwd,
                resumed=row is not None,
                title=(row or {}).get("title"),
                history=history,
                seed=self.query,
                notices=notices,
                store=spec.store,
                no_restore_cwd=self.args.no_restore_cwd or self.args.in_dir is not None,
                voice=self.chat.voice,
            )
            return entry(launch)
        finally:
            _stop_host(host, shutdown_terminal)


def _mcp_notices(scope: Any) -> list[str]:
    """The MCP startup summary when a server failed, is misconfigured or is cooling down."""
    from indus.service import mcp_host

    upkeep = mcp_host.upkeep_for(scope)
    report = upkeep.report if upkeep is not None else None
    if report is None or not (report.failed or report.problems or report.cooling_down):
        return []
    return [report.summary()]


def _stop_host(host: Any, shutdown_terminal: Any) -> None:
    """Close the sessions, shut the terminal runtime down on the host loop, stop the loop."""
    try:
        host.call(host.service.aclose(), timeout=30.0)
    except Exception:
        log.warning("closing the sessions failed", exc_info=True)
    try:
        host.call(shutdown_terminal(), timeout=30.0)
    except Exception:
        log.warning("terminal shutdown failed", exc_info=True)
    host.stop()
