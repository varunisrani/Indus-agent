"""The REPL's side of slash commands, the bang shell, ``/queue`` and Tab completion (P06-04).

The commands themselves are P06-08..11: ``indus.cli.slash.dispatch`` runs them against a
``SlashContext`` and answers with a ``SlashResult``; ``indus.cli.bang`` runs ``!command`` lines;
``indus.service.commands.registry.REGISTRY`` completes names. This module only turns those
answers into what the REPL does:

- ``exit`` → leave; ``clear_screen`` → clear; ``switch_to`` → follow the new session's events
  (the context already names it) and, when ``resumed``, run ``on_resume`` (cwd restore, recap,
  rehydrated switches — ``indus.cli.resume``); ``submit`` → send that text as a prompt;
  ``prefill`` → put that text in the draft.
- Plain lines first go to ``take_resume_pick`` so a number typed after ``/resume`` picks a row.
- At each turn end the REPL asks ``after_turn`` for the next queued text.

Slash and bang lines run in a *terminal section*: the prompt is put away (prompt_toolkit's
``run_in_terminal``), stdout is the real terminal again (so a bang command's output streams and a
question is shown before its answer is read), and Ctrl+C raises ``KeyboardInterrupt`` in the
command instead of reaching the prompt (a bang command is killed with 130). Inside a section the
context's ``confirm`` and ``ask_approval`` read a line from the terminal; outside one they answer
"no" / nothing, so a command can never hang a test or a pipe.

``wire_slash`` binds the REPL-side pieces of the context (``out``, ``is_busy``, ``confirm``,
``ask_approval``) and, after every command, copies what a command changed back to the REPL: the
reasoning display switch, the session's auto-approve flag, the title and (after ``/model``) the
model shown in the status bar.
"""

from __future__ import annotations

import contextlib
import logging
import signal
import sys
import threading
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.patch_stdout import StdoutProxy

from indus.cli import bang
from indus.cli.repl.app import CommandHook, Repl
from indus.cli.repl.cards import ApprovalCard
from indus.cli.slash.context import SlashContext, SlashResult
from indus.cli.slash.dispatch import (
    after_turn,
    dispatch,
    is_slash,
    runnable_names,
    take_resume_pick,
)
from indus.protocol.server_requests import ApprovalAnswer, ApprovalRequest
from indus.safety.approvals import APPROVALS, session_key
from indus.service.commands.registry import REGISTRY, command_word
from indus.service.commands.registry import Completion as Candidate
from indus.state import sessions

__all__ = [
    "SlashCompleter",
    "apply_result",
    "complete_for",
    "in_section",
    "slash_hook",
    "terminal_section",
    "wire_slash",
]

log = logging.getLogger(__name__)

type OnResume = Callable[[str], None]
type ReadLine = Callable[[str], str | None]

_section = threading.local()


def in_section() -> bool:
    """True while a terminal section runs on this thread."""
    return bool(getattr(_section, "active", False))


class SlashCompleter(Completer):
    """Tab completion of command names (and subcommands) for a line that starts with ``/``.

    ``complete(text)`` gives plain strings or the registry's ``Completion`` (text and a note)."""

    def __init__(self, complete: Callable[[str], Iterable[str | Candidate]]) -> None:
        self.complete = complete

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/") or "\n" in text:
            return
        word = text.rsplit(" ", 1)[-1]
        seen: set[str] = set()
        for item in self.complete(text):
            value, meta = (item, "") if isinstance(item, str) else (item.text, item.meta)
            if not value or value in seen:
                continue
            seen.add(value)
            if value.startswith("/") and " " not in text:
                yield Completion(value, start_position=-len(text), display_meta=meta or None)
            else:  # a subcommand or argument: replaces the word being typed
                yield Completion(value, start_position=-len(word), display_meta=meta or None)


def complete_for(ctx: SlashContext) -> Callable[[str], list[Candidate]]:
    """Completion over every name the context can run now (built-ins, quick, plugin, skill)."""

    def complete(text: str) -> list[Candidate]:
        names = runnable_names(ctx)
        return REGISTRY.complete(text, names, extra=names)

    return complete


# -- terminal sections --------------------------------------------------------------------------


@contextlib.contextmanager
def _real_streams() -> Iterator[None]:
    """Point stdout/stderr back at the terminal while prompt_toolkit's proxy is installed."""
    saved = sys.stdout, sys.stderr
    swap = isinstance(sys.stdout, StdoutProxy)
    if swap:
        sys.stdout = sys.__stdout__ or saved[0]
        if isinstance(sys.stderr, StdoutProxy):
            sys.stderr = sys.__stderr__ or saved[1]
    try:
        yield
    finally:
        if swap:
            with contextlib.suppress(Exception):
                sys.stdout.flush()
            sys.stdout, sys.stderr = saved


@contextlib.contextmanager
def _plain_ctrl_c() -> Iterator[None]:
    """Ctrl+C raises ``KeyboardInterrupt`` here (main thread only); the prompt's own handler and
    the event loop's wake-up pipe are put back afterwards, so the key is not seen twice."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    try:
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    except (ValueError, OSError):
        yield
        return
    wakeup = -1
    with contextlib.suppress(ValueError, OSError):
        wakeup = signal.set_wakeup_fd(-1)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError, OSError):
            signal.set_wakeup_fd(wakeup)
        with contextlib.suppress(ValueError, OSError, TypeError):
            signal.signal(signal.SIGINT, previous)


def _section_body[T](fn: Callable[[], T]) -> T:
    _section.active = True
    try:
        with _real_streams(), _plain_ctrl_c():
            return fn()
    finally:
        _section.active = False


def terminal_section[T](repl: Repl, fn: Callable[[], T], done: Callable[[T | None], None]) -> None:
    """Run ``fn`` with the prompt put away, then ``done(result)`` on the prompt's loop (``None``
    when Ctrl+C or an error ended it). Without a running prompt (tests, early start) ``fn`` runs
    at once."""

    def body() -> T | None:
        try:
            return _section_body(fn)
        except KeyboardInterrupt:
            repl.note("stopped")
            return None

    app = repl.app
    if app is None or not app.is_running:
        done(body())
        return
    future = run_in_terminal(body)

    def finished(fut: Any) -> None:
        try:
            result = fut.result()
        except BaseException as exc:
            log.exception("a command run in the terminal failed")
            repl.error(f"that command failed: {type(exc).__name__}: {exc}")
            result = None
        done(result)

    future.add_done_callback(finished)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]


def _read_line(prompt: str) -> str | None:
    """A line typed at the terminal inside a section; None outside one, or on Ctrl+C / EOF."""
    if not in_section():
        return None
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt, OSError, RuntimeError):
        return None


def confirm_with(read: ReadLine) -> Callable[[str], bool]:
    """A yes/no question answered on the terminal (anything but y/yes is no)."""

    def confirm(question: str) -> bool:
        answer = read(f"{question} [y/N] ")
        return answer is not None and answer.strip().lower() in ("y", "yes")

    return confirm


def approval_with(repl: Repl, read: ReadLine) -> Callable[[ApprovalRequest], ApprovalAnswer | None]:
    """The approval card for a bang command, answered on the terminal (digits as on the REPL's
    card; Ctrl+C or end of input means no answer, which the guard treats as a refusal)."""

    def ask(request: ApprovalRequest) -> ApprovalAnswer | None:
        card = ApprovalCard(request, repl.clock.monotonic(), 80)
        for line in card.body(80):
            repl.print(line)
        while True:
            typed = read(card.prompt(repl.clock.monotonic()))
            if typed is None:
                return None
            step = card.submit(typed)
            for line in step.show:
                repl.print(line)
            if isinstance(step.answer, ApprovalAnswer):
                return step.answer

    return ask


# -- results ------------------------------------------------------------------------------------


def apply_result(repl: Repl, result: SlashResult | None, on_resume: OnResume | None = None) -> None:
    """Carry out a ``SlashResult`` on ``repl``."""
    if result is None:
        return
    if result.clear_screen:
        repl.clear_screen()
    if result.switch_to:
        repl.attach(result.switch_to)
        if result.resumed and on_resume is not None:
            try:
                on_resume(result.switch_to)
            except Exception as exc:
                log.exception("resume follow-up failed")
                repl.error(f"could not finish resuming: {type(exc).__name__}: {exc}")
    if result.submit:
        repl.send(result.submit)
    if result.prefill:
        repl.prefill(result.prefill)
    if result.exit:
        repl.request_exit()


def sync_from(repl: Repl, ctx: SlashContext, word: str = "") -> None:
    """Copy what a command changed in the context back into the REPL (see the module notes)."""
    repl.renderer.show_reasoning = ctx.state.show_reasoning
    try:
        repl.auto_approve = APPROVALS.auto_approve(session_key(ctx.scope, ctx.session_id))
    except Exception:
        log.debug("could not read the auto-approve switch", exc_info=True)
    if ctx.store is not None:
        try:
            row = sessions.get(ctx.store, ctx.session_id)
        except Exception:
            row = None
        title = (row or {}).get("title") or ctx.state.pending_title or ""
        repl.title = str(title)
    if word in ("model", "provider"):
        try:
            repl.options.model = ctx.port.route(ctx.session_id).model
        except Exception:
            log.debug("could not read the session's model", exc_info=True)


def slash_hook(ctx: SlashContext, *, on_resume: OnResume | None = None) -> CommandHook:
    """The REPL command hook for slash commands and bang lines (see the module notes)."""

    def finish(repl: Repl, result: SlashResult | None, word: str) -> None:
        apply_result(repl, result, on_resume)
        sync_from(repl, ctx, word)

    def hook(repl: Repl, line: str) -> bool:
        if bang.is_bang(line):
            terminal_section(repl, lambda: bang.run_bang(line, ctx), lambda _code: None)
            return True
        if is_slash(line):
            word, _ = command_word(line)
            cmd = REGISTRY.resolve(word) if word else None
            name = cmd.name if cmd is not None else word
            terminal_section(
                repl, lambda: dispatch(line, ctx), lambda result: finish(repl, result, name)
            )
            return True
        picked = take_resume_pick(line, ctx)
        if picked is None:
            return False
        finish(repl, picked, "resume")
        return True

    return hook


def wire_slash(repl: Repl, ctx: SlashContext, *, on_resume: OnResume | None = None) -> None:
    """Plug the slash commands into ``repl`` (call before ``repl.run()``); binds the context's
    output, busy check and questions to this REPL."""
    ctx.out = repl.print
    ctx.is_busy = lambda: repl.busy
    ctx.confirm = confirm_with(_read_line)
    ctx.ask_approval = approval_with(repl, _read_line)
    ctx.state.show_reasoning = repl.renderer.show_reasoning
    repl.commands = slash_hook(ctx, on_resume=on_resume)
    repl.after_turn = lambda: after_turn(ctx)
    repl.completer = SlashCompleter(complete_for(ctx))
