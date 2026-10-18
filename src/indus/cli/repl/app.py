"""The classic REPL (P06-04): one prompt on the main thread, the turn on the host's loop thread.

``Repl.run()`` keeps a single prompt_toolkit prompt open for the whole session. The agent runs
behind the host (``InProcessHost`` in production, a fake in tests): turns are started with
``submit`` and reported back through the host's thread-safe feed. A pump thread reads the feed and
posts every item to the prompt's event loop, so all REPL state (busy flag, queued prompts, the
open card, the renderer) is only ever touched on that loop's thread. The REPL never opens the
store or runs a tool itself.

What the person types goes, in order: to an open card (approval, clarify, sudo, secret); to the
command hook for lines starting with ``/`` or ``!``; else to the model — or, while a turn runs, to
the busy-input route (``display.busy_input_mode``: interrupt, queue or steer).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import queue
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyPressEvent
from prompt_toolkit.output import Output
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style, merge_styles
from rich.console import Console, RenderableType
from rich.text import Text

from indus.cli.repl.cards import Card, CardStep, describe_answer, open_card
from indus.cli.repl.history import HISTORY_FILE, InputHistory
from indus.cli.repl.keys import (
    BusyMode,
    CtrlC,
    CtrlCPolicy,
    build_key_bindings,
    busy_mode,
    continue_line,
    continues_line,
    paste_preview,
)
from indus.cli.repl.render import SPINNER_FRAMES, MarkdownMode, StreamRenderer
from indus.cli.repl.status import STATUS_STYLE, StatusSnapshot, status_fragments
from indus.cli.repl.textutil import one_line, shorten
from indus.foundation import config
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.protocol.errors import SESSION_BUSY
from indus.protocol.events import (
    CancelReason,
    EventBase,
    ServerRequestCancelled,
    TurnDone,
    TurnStarted,
)
from indus.protocol.methods import (
    PromptSubmitResult,
    ProtocolError,
    RedirectOutcome,
    ServerRequestAnswerResult,
    SessionInterruptResult,
    SessionRedirectResult,
    SessionSteerResult,
)
from indus.protocol.requests_port import AnswerModel
from indus.protocol.server_requests import ServerRequestBase
from indus.safety.redaction import redact_output

if TYPE_CHECKING:
    from indus.cli.slash.context import SlashContext

__all__ = ["CommandHook", "Repl", "ReplHost", "ReplOptions", "run_repl"]

log = logging.getLogger(__name__)

ECHO_CHARS = 80
REFRESH_BOUNDS = (0.1, 60.0)

_WITHDRAWN: Mapping[CancelReason, str] = {
    CancelReason.TIMED_OUT: "no answer in time — the question was withdrawn",
    CancelReason.INTERRUPTED: "question withdrawn: the reply was interrupted",
    CancelReason.TURN_ENDED: "question withdrawn: the reply ended",
    CancelReason.ANSWERED_ELSEWHERE: "question answered in another window",
    CancelReason.SESSION_CLOSED: "question withdrawn: the session closed",
}

_BUSY_HINTS: Mapping[BusyMode, str] = {
    "interrupt": "a message sent during a reply redirects or interrupts it",
    "queue": "a message sent during a reply waits until the reply ends",
    "steer": "a message sent during a reply is handed to it as extra guidance",
}

_PROMPT_STYLE = Style.from_dict(
    {
        "prompt": "bold #7fb4e8",
        "prompt.busy": "#8a93a6",
        "prompt.card": "bold #e0b23c",
    }
)


class ReplHost(Protocol):
    """The blocking session calls the REPL needs (``InProcessHost`` has them all)."""

    def submit(self, session_id: str, text: str) -> PromptSubmitResult: ...

    def interrupt(
        self, session_id: str, generation: int | None = None, *, hard: bool = False
    ) -> SessionInterruptResult: ...

    def steer(self, session_id: str, text: str) -> SessionSteerResult: ...

    def redirect(self, session_id: str, text: str) -> SessionRedirectResult: ...

    def answer_server_request(
        self, session_id: str, request_id: str, answer: Mapping[str, Any] | AnswerModel
    ) -> ServerRequestAnswerResult: ...

    def feed(self, session_id: str) -> tuple[queue.Queue[Any], Callable[[], None]]: ...


type CommandHook = Callable[[Repl, str], bool]
"""Called with every entered line before it goes to the model; returns True when it handled the
line (a slash command, a bang command, a pick from an armed resume list)."""


@dataclass
class ReplOptions:
    """Settings of one REPL run (``from_scope`` reads them from the config)."""

    busy_mode: BusyMode = "interrupt"
    final_markdown: MarkdownMode = "render"
    show_reasoning: bool = False
    refresh_interval: float = 1.0
    history_path: Path | None = None
    model: str = ""
    title: str = ""
    auto_approve: bool = False
    busy_hint_seen: bool = False
    bell: bool = True
    multiline_shortcuts: bool = True
    """Ctrl+J inserts a newline; False makes it send (``display.cli_multiline_shortcuts``)."""

    @classmethod
    def from_scope(cls, scope: Scope, **overrides: Any) -> ReplOptions:
        """Options from the scope's config; unreadable or odd values fall back to defaults."""
        values: dict[str, Any] = {"history_path": scope.home / HISTORY_FILE}
        try:
            view = config.load(scope)
        except Exception:
            log.warning("could not read the config for the REPL; using defaults", exc_info=True)
            view = None
        if view is not None:
            get = view.get
            values["busy_mode"] = busy_mode(get("display.busy_input_mode", "interrupt"))
            markdown = str(get("display.final_response_markdown", "render")).strip().lower()
            known = markdown in ("render", "strip", "raw")
            values["final_markdown"] = markdown if known else "render"
            values["show_reasoning"] = get("display.show_reasoning", False) is True
            try:
                refresh = float(get("display.cli_refresh_interval", 1.0))
            except (TypeError, ValueError):
                refresh = 1.0
            low, high = REFRESH_BOUNDS
            values["refresh_interval"] = min(max(refresh, low), high)
            values["busy_hint_seen"] = get("onboarding.seen.busy_input", False) is True
            values["bell"] = get("display.bell_on_prompt", True) is not False
            values["multiline_shortcuts"] = (
                get("display.cli_multiline_shortcuts", True) is not False
            )
        values.update(overrides)
        return cls(**values)


class _Stop:
    """Tells a pump thread to finish."""


_STOP = _Stop()


class Repl:
    """The classic chat loop (see the module notes)."""

    def __init__(
        self,
        host: ReplHost,
        session_id: str,
        *,
        scope: Scope,
        options: ReplOptions | None = None,
        commands: CommandHook | None = None,
        console: Console | None = None,
        input: Input | None = None,  # noqa: A002 - prompt_toolkit's own name
        output: Output | None = None,
        clock: Clock | None = None,
        status_source: Callable[[], StatusSnapshot] | None = None,
        use_patch_stdout: bool | None = None,
        emoji_lookup: Callable[[str], str | None] | None = None,
        after_turn: Callable[[], str | None] | None = None,
        completer: Completer | None = None,
    ) -> None:
        self.host = host
        self.session_id = session_id
        self.scope = scope
        self.options = options or ReplOptions()
        self.commands = commands
        self.after_turn = after_turn
        """Asked for the next queued text at each turn end once ``pending`` is empty."""
        self.completer = completer
        """Tab completion for the prompt (slash command names); set before ``run``."""
        self.console = console or Console(highlight=False)
        self.clock = clock or scope.clock
        self.status_source = status_source
        self.renderer = StreamRenderer(
            self.console,
            show_reasoning=self.options.show_reasoning,
            final_markdown=self.options.final_markdown,
            emoji_lookup=emoji_lookup,
            redact=self._redact,
        )
        self.ctrl_c = CtrlCPolicy(self.clock)
        self.pending: deque[str] = deque()
        """Prompts waiting for the running turn to end (sent one per ended turn)."""
        self.title = self.options.title
        self.auto_approve = self.options.auto_approve
        self.busy = False
        self.turn_id: str | None = None
        self.generation: int | None = None
        self.card: Card | None = None
        self._waiting: deque[ServerRequestBase] = deque()
        self._held_draft: Document | None = None
        self._turn_started: float | None = None
        self._last_turn_seconds: float | None = None
        self._input = input
        self._output = output
        self._patch = use_patch_stdout if use_patch_stdout is not None else console is None
        self._session: PromptSession[int] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._post_lock = threading.Lock()
        self._early: list[Callable[[], None]] = []
        self.ready = threading.Event()
        """Set once the prompt is running and posted work is executed."""
        self._feed: queue.Queue[Any] | None = None
        self._detach: Callable[[], None] | None = None
        self._pump: threading.Thread | None = None
        self._attach_count = 0
        self._exit_requested = False

    def _redact(self, text: str) -> str:
        return redact_output(text, scope=self.scope)

    # -- running ------------------------------------------------------------------------------

    def run(self) -> int:
        """Show the prompt until the person leaves; returns the exit code (0)."""
        self.attach(self.session_id)
        history = InputHistory(self.options.history_path) if self.options.history_path else None
        session: PromptSession[int] = PromptSession(
            message=self._message,
            bottom_toolbar=self._toolbar,
            key_bindings=build_key_bindings(self),
            history=history,
            input=self._input,
            output=self._output,
            multiline=True,
            is_password=Condition(lambda: self.masked),
            prompt_continuation=lambda width, line, wrap: " " * max(0, width),
            style=merge_styles([STATUS_STYLE, _PROMPT_STYLE]),
            refresh_interval=self.options.refresh_interval,
            enable_history_search=False,
            completer=self.completer,
            complete_while_typing=False,
        )
        self._session = session
        try:
            with self._stdout_patch():
                result = session.prompt(pre_run=self._started)
        except (EOFError, KeyboardInterrupt):
            result = 0
        finally:
            with self._post_lock:
                self._loop = None
            self._stop_pump()
        return result if isinstance(result, int) else 0

    @contextlib.contextmanager
    def _stdout_patch(self) -> Iterator[None]:
        if self._patch:
            with patch_stdout(raw=True):
                yield
        else:
            yield

    def _started(self) -> None:
        loop = asyncio.get_running_loop()
        app = self.app
        if app is not None:
            # A terminal that never answers cursor-position requests only costs a little layout
            # precision; do not print prompt_toolkit's warning into the chat.
            app.renderer.cpr_not_supported_callback = None
            app.create_background_task(self._ticker())
        with self._post_lock:
            self._loop = loop
            early, self._early = self._early, []
        for job in early:
            job()
        self.ready.set()
        if self._exit_requested:
            self.request_exit()

    async def _ticker(self) -> None:
        while True:
            await self.clock.sleep(1.0)
            self.tick()

    @property
    def masked(self) -> bool:
        """True while typed text must be hidden (a sudo or secret card is open)."""
        return self.card is not None and self.card.masked

    @property
    def prompt_session(self) -> PromptSession[int] | None:
        return self._session

    @property
    def app(self) -> Application[int] | None:
        return self._session.app if self._session is not None else None

    def post(self, fn: Callable[[], Any]) -> concurrent.futures.Future[Any]:
        """Run ``fn`` on the prompt's loop thread; the future carries its result."""
        done: concurrent.futures.Future[Any] = concurrent.futures.Future()

        def job() -> None:
            try:
                done.set_result(fn())
            except BaseException as exc:
                done.set_exception(exc)

        with self._post_lock:
            loop = self._loop
            if loop is None:
                self._early.append(job)
                return done
        try:
            loop.call_soon_threadsafe(job)
        except RuntimeError:  # the loop closed meanwhile
            done.cancel()
        return done

    def request_exit(self) -> None:
        """Leave the REPL after the current key or command."""
        self._exit_requested = True
        app = self.app
        if app is not None and app.is_running and app.future is not None and not app.future.done():
            app.exit(result=0)

    # -- the feed -------------------------------------------------------------------------------

    def attach(self, session_id: str) -> None:
        """Follow ``session_id``'s events from now on (start, ``/new``, ``/resume``)."""
        self._stop_pump()
        self.session_id = session_id
        self.busy = False
        self.turn_id = None
        self.generation = None
        self.pending.clear()
        self._waiting.clear()
        feed, detach = self.host.feed(session_id)
        self._feed, self._detach = feed, detach
        self._attach_count += 1
        token = self._attach_count
        thread = threading.Thread(
            target=self._pump_feed, args=(feed, token), name="indus-repl-feed", daemon=True
        )
        self._pump = thread
        thread.start()

    def _pump_feed(self, feed: queue.Queue[Any], token: int) -> None:
        while True:
            item = feed.get()
            if isinstance(item, _Stop):
                return
            self.post(lambda item=item: self._from_feed(item, token))

    def _stop_pump(self) -> None:
        feed, detach, pump = self._feed, self._detach, self._pump
        self._feed = self._detach = self._pump = None
        if detach is not None:
            try:
                detach()
            except Exception:
                log.warning("could not detach the session feed", exc_info=True)
        if feed is not None:
            feed.put(_STOP)
        if pump is not None and pump is not threading.current_thread():
            pump.join(5.0)

    def _from_feed(self, item: object, token: int) -> None:
        if token == self._attach_count:
            self.handle_item(item)

    def handle_item(self, item: object) -> None:
        """React to one feed item (an event or a server request); loop thread only."""
        if isinstance(item, ServerRequestBase):
            self._request_arrived(item)
        elif isinstance(item, ServerRequestCancelled):
            self.renderer.handle(item)
            self._request_withdrawn(item)
        elif isinstance(item, EventBase):
            if isinstance(item, TurnStarted):
                self.busy = True
                self.turn_id = item.turn_id
                self.generation = item.generation
                if self._turn_started is None:
                    self._turn_started = self.clock.monotonic()
            self.renderer.handle(item)
            if isinstance(item, TurnDone) and self.turn_id in (item.turn_id, None):
                self._turn_ended()
        self._invalidate()

    def _turn_ended(self) -> None:
        if self._turn_started is not None:
            self._last_turn_seconds = self.clock.monotonic() - self._turn_started
        self._turn_started = None
        self.busy = False
        self.turn_id = None
        self.generation = None
        self._drain_pending()

    def _invalidate(self) -> None:
        app = self.app
        if app is not None and app.is_running:
            app.invalidate()

    # -- output helpers ------------------------------------------------------------------------

    def print(self, renderable: RenderableType | str, *, style: str = "") -> None:
        """Print a line or renderable above the prompt (strings are never read as markup)."""
        self.renderer.emit(renderable, style=style)

    def note(self, text: str) -> None:
        self.print(f"  {text}", style="dim")

    def error(self, text: str) -> None:
        self.print(f"  ✗ {text}", style="red")

    def _echo(self, text: str) -> None:
        lines = text.split("\n")
        shown = Text("› ", style="bold cyan")
        shown.append(shorten(lines[0], ECHO_CHARS), style="bold")
        if len(lines) > 1:
            shown.append(f"  (+{len(lines) - 1} more lines)", style="dim")
        self.print("")
        self.print(shown)

    # -- sending --------------------------------------------------------------------------------

    def dispatch(self, text: str) -> None:
        """Route one entered line: command hook, then the model (or the busy route)."""
        if self.commands is not None:
            try:
                if self.commands(self, text):
                    return
            except Exception as exc:
                log.exception("command failed")
                self.error(f"that command failed: {type(exc).__name__}: {exc}")
                return
        elif text.strip().lower() in ("/quit", "/exit"):
            self.request_exit()
            return
        self.send(text)

    def prefill(self, text: str) -> None:
        """Put ``text`` in the draft for editing (``/undo`` hands back the last message)."""
        buffer = self._buffer()
        if buffer is not None:
            buffer.reset(document=Document(text, len(text)))

    def clear_screen(self) -> None:
        """Clear the terminal (``/clear``); the output ring is emptied too."""
        self.renderer.ring.clear()
        app = self.app
        if app is not None and app.is_running:
            app.renderer.clear()

    def send(self, text: str) -> None:
        """Send ``text`` to the model now, or route it while a turn runs."""
        self._echo(text)
        if self.busy:
            self._busy_input(text)
        else:
            self._submit(text)

    def queue_prompt(self, text: str) -> None:
        """Hold ``text`` until the running turn ends (sent at once when idle)."""
        if self.busy:
            self.pending.append(text)
        else:
            self._submit(text)

    def _submit(self, text: str) -> None:
        try:
            ticket = self.host.submit(self.session_id, text)
        except ProtocolError as exc:
            if exc.error == SESSION_BUSY:
                self.pending.appendleft(text)
                self.busy = True
                self.note("the session is busy; your message goes out when it is free")
            else:
                self.error(exc.message)
            return
        self.busy = True
        self.turn_id = ticket.turn_id
        self.generation = ticket.generation
        self._turn_started = self.clock.monotonic()

    def _drain_pending(self) -> None:
        if self.busy:
            return
        if self.pending:
            text = self.pending.popleft()
        else:
            text = self._next_from_after_turn()
            if text is None:
                return
        self.note(f"sending queued message: {shorten(one_line(text), 60)}")
        self._submit(text)

    def _next_from_after_turn(self) -> str | None:
        if self.after_turn is None:
            return None
        try:
            text = self.after_turn()
        except Exception:
            log.exception("the after-turn hook failed")
            return None
        return text if text and text.strip() else None

    def _busy_input(self, text: str) -> None:
        mode = self.options.busy_mode
        preview = shorten(one_line(text), 60)
        if mode == "steer":
            try:
                queued = self.host.steer(self.session_id, text).queued
            except ProtocolError:
                queued = False
            if queued:
                self.note(f"handed to the running reply: {preview}")
            else:
                self.pending.append(text)
                self.note("queued: it goes out when this reply ends")
        elif mode == "queue":
            self.pending.append(text)
            self.note("queued: it goes out when this reply ends")
        else:
            try:
                outcome = self.host.redirect(self.session_id, text).outcome
            except ProtocolError:
                outcome = RedirectOutcome.REJECTED
            if outcome is RedirectOutcome.REDIRECTED:
                self.note("redirecting the reply with your message")
            elif outcome is RedirectOutcome.STEERED:
                self.note("your message reaches the reply after the running tool")
            else:
                self.pending.appendleft(text)
                self.interrupt()
                self.note("interrupted; your message goes out next")
        self._busy_hint(mode)

    def _busy_hint(self, mode: BusyMode) -> None:
        if self.options.busy_hint_seen:
            return
        self.options.busy_hint_seen = True
        self.note(
            f"tip: {_BUSY_HINTS[mode]}. Set display.busy_input_mode to interrupt, queue or "
            "steer to change this."
        )
        try:
            config.save(self.scope, {"onboarding": {"seen": {"busy_input": True}}})
        except Exception as exc:
            log.info("could not remember the busy-input hint: %s", type(exc).__name__)

    def interrupt(self) -> bool:
        """Stop the running turn (its generation only) and seal the partial reply."""
        if not self.busy:
            return False
        try:
            result = self.host.interrupt(self.session_id, self.generation)
        except ProtocolError as exc:
            self.error(f"could not interrupt: {exc.message}")
            return False
        self.renderer.seal()
        self._invalidate()
        return result.interrupted

    # -- cards ----------------------------------------------------------------------------------

    def _request_arrived(self, request: ServerRequestBase) -> None:
        if self.card is None:
            self._open_card(request)
        elif request.request_id != self.card.request_id and all(
            r.request_id != request.request_id for r in self._waiting
        ):
            self._waiting.append(request)

    def _open_card(self, request: ServerRequestBase) -> None:
        card = open_card(request, self.clock.monotonic(), self._width())
        if card is None:
            self.note(f"cannot show a {type(request).__name__} here")
            return
        self.card = card
        buffer = self._buffer()
        if buffer is not None:
            self._held_draft = buffer.document if buffer.text else None
            buffer.reset()
        for line in card.body(self._width()):
            self.print(line)
        app = self.app
        if self.options.bell and app is not None and app.is_running:
            app.output.bell()

    def _close_card(self) -> None:
        self.card = None
        buffer = self._buffer()
        if buffer is not None:
            buffer.reset(document=self._held_draft or Document())
        self._held_draft = None
        if self._waiting:
            self._open_card(self._waiting.popleft())

    def _answer_card(self, answer: AnswerModel) -> None:
        card = self.card
        if card is None:
            return
        try:
            self.host.answer_server_request(self.session_id, card.request_id, answer)
            self.note(f"→ {describe_answer(answer)}")
        except ProtocolError:
            self.note("that question was already closed")
        self._close_card()

    def _card_step(self, step: CardStep) -> None:
        for line in step.show:
            self.print(line)
        if step.answer is not None:
            self._answer_card(step.answer)

    def _request_withdrawn(self, event: ServerRequestCancelled) -> None:
        if self.card is not None and self.card.request_id == event.request_id:
            self.note(_WITHDRAWN.get(event.reason, "question withdrawn"))
            self._close_card()
            return
        self._waiting = deque(r for r in self._waiting if r.request_id != event.request_id)

    def tick(self) -> None:
        """Periodic check (each second): close a card whose time ran out."""
        card = self.card
        if card is None:
            return
        deadline = card.deadline()
        if deadline is not None and self.clock.monotonic() >= deadline:
            self.note("no answer — the question timed out and nothing was approved")
            self._close_card()
            self._invalidate()

    # -- keys (KeyTarget) ----------------------------------------------------------------------

    def _buffer(self) -> Buffer | None:
        app = self.app
        return app.current_buffer if app is not None else None

    @property
    def multiline_shortcuts(self) -> bool:
        return self.options.multiline_shortcuts

    def card_takes_digits(self) -> bool:
        buffer = self._buffer()
        return self.card is not None and self.card.takes_digits and not (buffer and buffer.text)

    def key_enter(self, event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        if self.card is not None:
            text = buffer.text
            buffer.reset()
            self._card_step(self.card.submit(text))
            return
        text = buffer.text
        if continues_line(text):
            buffer.text = continue_line(text)
            buffer.cursor_position = len(buffer.text)
            return
        if not text.strip():
            return
        buffer.reset(append_to_history=True)
        self.dispatch(text)

    def key_digit(self, event: KeyPressEvent, digit: int) -> None:
        if self.card is not None:
            self._card_step(self.card.on_digit(digit))

    def key_ctrl_c(self, event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        action = self.ctrl_c.press(
            card_open=self.card is not None, draft=bool(buffer.text), busy=self.busy
        )
        match action:
            case CtrlC.CANCEL_CARD:
                buffer.reset()
                if self.card is not None:
                    self._answer_card(self.card.cancel())
            case CtrlC.CLEAR_DRAFT:
                buffer.reset()
            case CtrlC.INTERRUPT:
                self.interrupt()
                self.note("stopping the reply — press Ctrl+C again within 2 s to quit")
            case CtrlC.EXIT:
                event.app.exit(result=0)
            case CtrlC.HINT:
                self.note("press Ctrl+C again to quit (or Ctrl+D on an empty line)")

    def key_paste(self, event: KeyPressEvent) -> None:
        data = event.data.replace("\r\n", "\n").replace("\r", "\n")
        event.current_buffer.insert_text(data)
        if self.card is None or not self.card.masked:
            preview = paste_preview(data)
            if preview is not None:
                self.note(preview)

    # -- prompt and toolbar --------------------------------------------------------------------

    def _width(self) -> int:
        app = self.app
        if app is not None:
            try:
                return max(20, app.output.get_size().columns)
            except Exception:
                return 80
        return 80

    def _message(self) -> StyleAndTextTuples:
        if self.card is not None:
            return [("class:prompt.card", self.card.prompt(self.clock.monotonic()))]
        if self.busy:
            return [("class:prompt.busy", "› ")]
        return [("class:prompt", "› ")]

    def snapshot(self) -> StatusSnapshot:
        """The status bar's data now."""
        base: StatusSnapshot | None = None
        if self.status_source is not None:
            try:
                base = self.status_source()
            except Exception:
                log.debug("status source failed", exc_info=True)
        if base is None:
            base = StatusSnapshot(model=self.options.model)
        live = self._turn_started is not None
        seconds = (
            self.clock.monotonic() - self._turn_started
            if self._turn_started is not None
            else self._last_turn_seconds
        )
        return replace(
            base,
            title=base.title or self.title,
            auto_approve=base.auto_approve or self.auto_approve,
            turn_seconds=seconds,
            turn_live=live,
        )

    def _toolbar(self) -> StyleAndTextTuples:
        width = self._width()
        try:
            out: StyleAndTextTuples = []
            if self.busy:
                frame = SPINNER_FRAMES[int(self.clock.monotonic()) % len(SPINNER_FRAMES)]
                activity = self.renderer.activity()
                out.append(("class:status.activity", shorten(f" {frame} {activity}", width)))
                out.append(("", "\n"))
            out.extend(status_fragments(self.snapshot(), width))
            return out
        except Exception:
            log.debug("status bar failed", exc_info=True)
            return [("", f" {self.options.model or 'indus'}")]


def run_repl(
    host: ReplHost,
    session_id: str,
    *,
    scope: Scope,
    options: ReplOptions | None = None,
    commands: CommandHook | None = None,
    banner: RenderableType | None = None,
    recap: RenderableType | None = None,
    slash_ctx: SlashContext | None = None,
    on_resume: Callable[[str], None] | None = None,
    on_ready: Callable[[Repl], None] | None = None,
    **kwargs: Any,
) -> int:
    """Print the banner and recap, then run the REPL until the person leaves (exit code 0).

    ``slash_ctx`` (the classic CLI's ``SlashContext``) wires slash commands, the bang shell,
    ``/queue`` and Tab completion through ``slash_bridge.wire_slash``; ``on_resume(session_id)``
    runs after a command resumed another session (``indus.cli.resume``). ``on_ready(repl)`` runs
    once the REPL is built and wired, before the banner (``repl.post`` work runs once the prompt
    is up). ``kwargs`` go to ``Repl`` (``console``, ``input``, ``output``, ``clock``,
    ``status_source``, ``emoji_lookup``, ``use_patch_stdout``)."""
    repl = Repl(
        host,
        session_id,
        scope=scope,
        options=options or ReplOptions.from_scope(scope),
        commands=commands,
        **kwargs,
    )
    if slash_ctx is not None:
        from indus.cli.repl.slash_bridge import wire_slash

        wire_slash(repl, slash_ctx, on_resume=on_resume)
    if on_ready is not None:
        on_ready(repl)
    if banner is not None:
        repl.print(banner)
    if recap is not None:
        repl.print(recap)
    return repl.run()
