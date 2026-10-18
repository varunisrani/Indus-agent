"""Streaming render for the classic REPL (P06-05).

``StreamRenderer`` turns a session's protocol events into lines on a rich ``Console``:

- reply text (``message.delta``) is printed a whole line at a time as soon as the line ends; the
  unfinished tail waits for its newline or the end of the turn and is offered to the status line
  meanwhile (``activity``);
- reasoning is hidden unless ``show_reasoning`` is on, then printed dim the same way;
- a tool call shows on the status line while it runs and leaves one line when it finishes:
  mark, emoji, name, a short preview of its main argument and how long it took;
- notices print one line; retry and compression statuses print their text;
- ``seal()`` (the user interrupted) prints what is buffered at once with an interrupted marker,
  and every later delta, reasoning piece or tool line of that turn is dropped (R05.4);
- ``turn.done`` flushes what is left and reports a failure or an unusual ending.

Text from the model is never read as rich markup. ``final_markdown="strip"`` removes markdown
syntax line by line. The last ``RING_SIZE`` printed lines are kept in ``ring`` for a redraw.
"""

from __future__ import annotations

import io
import re
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from rich.console import Console, RenderableType
from rich.text import Text

from indus.cli.repl.textutil import clean, one_line, shorten
from indus.protocol.events import (
    EventBase,
    ExitReason,
    MessageDelta,
    Notice,
    NoticeLevel,
    ReasoningDelta,
    Status,
    StatusKind,
    ToolCompleted,
    ToolProgress,
    ToolStarted,
    TurnDone,
    TurnStarted,
)

__all__ = [
    "RING_SIZE",
    "SPINNER_FRAMES",
    "SPINNER_VERBS",
    "TOOL_EMOJI",
    "MarkdownMode",
    "StreamRenderer",
    "argument_preview",
    "format_elapsed",
    "strip_markdown_line",
    "tool_emoji",
]

RING_SIZE = 200
PREVIEW_CHARS = 60
PARTIAL_PREVIEW_CHARS = 48

type MarkdownMode = Literal["render", "strip", "raw"]

SPINNER_FRAMES = ("◐", "◓", "◑", "◒")
SPINNER_VERBS = ("thinking", "working on it", "mulling it over", "putting it together")

TOOL_EMOJI: Mapping[str, str] = {
    "terminal": "💻",
    "process": "🔁",
    "read_file": "📖",
    "write_file": "✏️",
    "patch": "🩹",
    "search_files": "🔎",
    "web_search": "🌐",
    "web_extract": "📄",
    "todo": "📝",
    "clarify": "❓",
}
DEFAULT_TOOL_EMOJI = "🔧"

# Arguments worth previewing, best first.
_PREVIEW_KEYS = ("command", "path", "query", "url", "pattern", "file", "name", "text")

_ENDINGS: Mapping[ExitReason, str] = {
    ExitReason.BUDGET_EXHAUSTED: "stopped: this turn used up its budget",
    ExitReason.MAX_ITERATIONS: "stopped: the turn reached its step limit",
    ExitReason.INVALID_TOOL_STRIKES: "stopped: the model kept calling tools that do not exist",
    ExitReason.PERSISTENCE_FAILED: "stopped: the conversation could not be saved",
    ExitReason.PROVIDER_FAILED: "stopped: the model provider failed",
    ExitReason.COMPRESSION_EXHAUSTED: "stopped: the conversation no longer fits the model",
    ExitReason.GUARDRAIL_HALT: "stopped by a safety check",
}
_INTERRUPTED = frozenset({ExitReason.INTERRUPTED_BY_USER, ExitReason.INTERRUPTED_BY_SYSTEM})

INTERRUPTED_MARK = "(interrupted)"


def tool_emoji(name: str, lookup: Callable[[str], str | None] | None = None) -> str:
    """The emoji shown for tool ``name``: ``lookup`` first, then our table, then a wrench."""
    if lookup is not None:
        found = lookup(name)
        if found:
            return found
    return TOOL_EMOJI.get(name, DEFAULT_TOOL_EMOJI)


def argument_preview(
    arguments: Mapping[str, object],
    limit: int = PREVIEW_CHARS,
    redact: Callable[[str], str] | None = None,
) -> str:
    """A one-line preview of the call's main argument (the first string one when none of the
    usual keys is present), masked by ``redact`` before it is shortened so a cut never splits a
    secret the masker would have recognised."""
    chosen = ""
    for key in _PREVIEW_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            chosen = value
            break
    else:
        for value in arguments.values():
            if isinstance(value, str) and value.strip():
                chosen = value
                break
    if not chosen:
        return ""
    text = one_line(chosen)
    if redact is not None:
        text = redact(text)
    return shorten(text, limit)


def format_elapsed(ms: int) -> str:
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m {rest:02d}s"


# -- markdown strip ---------------------------------------------------------------------------

_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_RULE = re.compile(r"^\s{0,3}(?:(?:-\s*){3,}|(?:_\s*){3,}|(?:\*\s*){3})$")
_HEADING = re.compile(r"^(\s{0,3})#{1,6}\s+")
_INLINE = (
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"\*\*\*(\S(?:.*?\S)?)\*\*\*"), r"\1"),
    (re.compile(r"\*\*(\S(?:.*?\S)?)\*\*"), r"\1"),
    (re.compile(r"(?<!\w)__(\S(?:.*?\S)?)__(?!\w)"), r"\1"),
    (re.compile(r"(?<![\w*])\*(\S(?:[^*]*?\S)?)\*(?![\w*])"), r"\1"),
    (re.compile(r"(?<!\w)_(\S(?:[^_]*?\S)?)_(?!\w)"), r"\1"),
    (re.compile(r"~~(\S(?:.*?\S)?)~~"), r"\1"),
)


def strip_markdown_line(line: str) -> str | None:
    """``line`` without markdown syntax; None for a line that is only a code fence. Lists and
    quotes keep their markers: they carry structure."""
    if _FENCE.match(line):
        return None
    if _RULE.match(line):
        return ""
    line = _HEADING.sub(r"\1", line)
    for pattern, repl in _INLINE:
        line = pattern.sub(repl, line)
    return line


# -- renderer ---------------------------------------------------------------------------------


def _plain_render(renderable: RenderableType, width: int) -> str:
    """``renderable`` as plain text at ``width`` columns (for the output ring)."""
    sink = Console(file=io.StringIO(), width=width, color_system=None, force_terminal=False)
    sink.print(renderable)
    file = sink.file
    assert isinstance(file, io.StringIO)
    return file.getvalue()


@dataclass
class _ToolRun:
    name: str
    preview: str


class StreamRenderer:
    """Renders one session's events (see the module notes). Thread-safe: one lock guards it."""

    def __init__(
        self,
        console: Console,
        *,
        show_reasoning: bool = False,
        final_markdown: MarkdownMode = "render",
        emoji_lookup: Callable[[str], str | None] | None = None,
        redact: Callable[[str], str] | None = None,
        ring_size: int = RING_SIZE,
    ) -> None:
        self.console = console
        self.show_reasoning = show_reasoning
        self.final_markdown: MarkdownMode = final_markdown
        self.emoji_lookup = emoji_lookup
        self.redact = redact
        """Masks secrets in tool argument previews (tool.started carries raw arguments)."""
        self.ring: deque[str] = deque(maxlen=ring_size)
        self._lock = threading.RLock()
        self._turn_id: str | None = None
        self._sealed: set[str] = set()
        self._reply_open = False
        self._reasoning_open = False
        self._line = ""
        self._thinking = ""
        self._tools: dict[str, _ToolRun] = {}
        self._status: StatusKind | None = None
        self._status_text = ""
        self._last_notice: tuple[str | None, str] | None = None
        self._turns = 0
        self._printed_in_turn = False

    # -- output -----------------------------------------------------------------------------

    def emit(self, renderable: RenderableType | str, *, style: str = "") -> None:
        """Print ``renderable`` (a plain string is printed literally, never as markup)."""
        with self._lock:
            if isinstance(renderable, str):
                renderable = Text(renderable, style=style)
            if isinstance(renderable, Text):
                self.console.print(renderable, soft_wrap=True)
                plain = renderable.plain
            else:
                self.console.print(renderable)
                plain = _plain_render(renderable, self.console.width)
            self.ring.extend(plain.rstrip("\n").split("\n"))

    # -- state for the status line ---------------------------------------------------------

    @property
    def turn_id(self) -> str | None:
        return self._turn_id

    @property
    def partial(self) -> str:
        """The reply's unfinished last line."""
        return self._line

    def activity(self) -> str:
        """A short description of what the turn is doing right now (no spinner glyph)."""
        with self._lock:
            if self._tools:
                run = next(reversed(self._tools.values()))
                emoji = tool_emoji(run.name, self.emoji_lookup)
                return f"{emoji} {run.name} {run.preview}".rstrip()
            status = self._status
            if status is StatusKind.RETRYING or status is StatusKind.COMPRESSING:
                return self._status_text or status.value
            if self._line.strip():
                tail = one_line(self._line)
                if len(tail) > PARTIAL_PREVIEW_CHARS:
                    tail = "…" + tail[-(PARTIAL_PREVIEW_CHARS - 1) :]
                return tail
            if self._turn_id is None:
                return ""
            return SPINNER_VERBS[(self._turns - 1) % len(SPINNER_VERBS)] + "…"

    # -- events -----------------------------------------------------------------------------

    def handle(self, event: EventBase) -> None:
        """Render one event."""
        with self._lock:
            match event:
                case TurnStarted():
                    self._start(event.turn_id)
                case MessageDelta():
                    if self._live(event.turn_id):
                        self._reply(event.text)
                case ReasoningDelta():
                    if self._live(event.turn_id) and self.show_reasoning:
                        self._reason(event.text)
                case ToolStarted():
                    if self._live(event.turn_id):
                        self._flush_open_text()
                        args: Mapping[str, object] = event.arguments
                        preview = argument_preview(args, redact=self.redact)
                        self._tools[event.call_id] = _ToolRun(event.name, preview)
                case ToolProgress():
                    pass
                case ToolCompleted():
                    if self._live(event.turn_id):
                        self._tool_done(event)
                    else:
                        self._tools.pop(event.call_id, None)
                case Status():
                    self._on_status(event)
                case Notice():
                    self._on_notice(event)
                case TurnDone():
                    self._done(event)
                case _:
                    pass

    def seal(self) -> None:
        """The user interrupted: show what is buffered now, mark it, ignore the rest (R05.4)."""
        with self._lock:
            turn = self._turn_id
            if turn is None or turn in self._sealed:
                return
            self._flush_open_text()
            self._sealed.add(turn)
            self._tools.clear()
            self.emit(f"  {INTERRUPTED_MARK}", style="dim italic")

    # -- internals ----------------------------------------------------------------------------

    def _live(self, turn_id: str) -> bool:
        if self._turn_id is None and turn_id not in self._sealed:
            self._start(turn_id)  # joined mid-turn (resume): adopt it
        return turn_id == self._turn_id and turn_id not in self._sealed

    def _start(self, turn_id: str) -> None:
        if self._turn_id is not None and self._turn_id != turn_id:
            self._finish_text()
        self._turn_id = turn_id
        self._turns += 1
        self._reply_open = False
        self._reasoning_open = False
        self._line = ""
        self._thinking = ""
        self._tools.clear()
        self._status = StatusKind.WORKING
        self._status_text = ""
        self._printed_in_turn = False

    def _reply_line(self, line: str) -> None:
        line = clean(line)
        if self.final_markdown == "strip":
            stripped = strip_markdown_line(line)
            if stripped is None:
                return
            line = stripped
        self.emit(f"  {line}")

    def _reply(self, text: str) -> None:
        if not text:
            return
        self._close_reasoning()
        if not self._reply_open:
            text = text.lstrip("\n")
            if not text:
                return
            self.emit("")
            self.emit("◇ indus", style="bold cyan")
            self._reply_open = True
            self._printed_in_turn = True
        self._line += text
        while "\n" in self._line:
            line, self._line = self._line.split("\n", 1)
            self._reply_line(line)

    def _reason(self, text: str) -> None:
        if not text:
            return
        if not self._reasoning_open:
            self.emit("  ┆ reasoning", style="dim italic")
            self._reasoning_open = True
            self._printed_in_turn = True
        self._thinking += text
        while "\n" in self._thinking:
            line, self._thinking = self._thinking.split("\n", 1)
            self.emit(f"  ┆ {clean(line)}", style="dim")

    def _close_reasoning(self) -> None:
        if self._thinking:
            self.emit(f"  ┆ {clean(self._thinking)}", style="dim")
            self._thinking = ""
        self._reasoning_open = False

    def _flush_open_text(self) -> None:
        """Print the held partial lines (before a tool line, a seal or the turn's end)."""
        self._close_reasoning()
        if self._line:
            self._reply_line(self._line)
            self._line = ""
        self._reply_open = False

    def _finish_text(self) -> None:
        self._flush_open_text()

    def _tool_done(self, event: ToolCompleted) -> None:
        self._flush_open_text()
        run = self._tools.pop(event.call_id, None) or _ToolRun(event.name, "")
        mark, style = ("✓", "green") if event.ok else ("✗", "red")
        line = Text("  ")
        line.append(mark, style=style)
        line.append(f" {tool_emoji(run.name, self.emoji_lookup)} ")
        line.append(run.name, style="bold")
        if run.preview:
            line.append(f"  {run.preview}", style="dim")
        line.append(f"  {format_elapsed(event.duration_ms)}", style="dim")
        self.emit(line)
        self._printed_in_turn = True

    def _on_status(self, event: Status) -> None:
        self._status = event.kind
        self._status_text = one_line(event.text)
        if event.kind is StatusKind.RETRYING and self._status_text:
            self.emit(f"  ↻ {self._status_text}", style="dim")
        elif event.kind is StatusKind.COMPRESSING:
            self.emit(f"  ⇣ {self._status_text or 'compressing the conversation'}", style="dim")

    def _on_notice(self, event: Notice) -> None:
        text = one_line(event.text)
        signature = (event.key, text)
        if event.key is not None and signature == self._last_notice:
            return
        self._last_notice = signature
        if event.level is NoticeLevel.ERROR:
            self.emit(f"  ✗ {text}", style="red")
        elif event.level is NoticeLevel.WARNING:
            self.emit(f"  ! {text}", style="yellow")
        else:
            self.emit(f"  · {text}", style="dim")

    def _done(self, event: TurnDone) -> None:
        sealed = event.turn_id in self._sealed
        if event.turn_id == self._turn_id and not sealed:
            self._flush_open_text()
            if event.reason in _INTERRUPTED:
                self.emit(f"  {INTERRUPTED_MARK}", style="dim italic")
        if event.failure is not None:
            line = f"  ✗ {one_line(event.failure.reason)}"
            if event.failure.retryable:
                line += " (trying again later may work)"
            self.emit(line, style="red")
        elif event.reason in _ENDINGS:
            self.emit(f"  ! {_ENDINGS[event.reason]}", style="yellow")
        elif (
            event.reason is ExitReason.COMPLETED
            and event.turn_id == self._turn_id
            and not self._printed_in_turn
        ):
            self.emit("  (the reply had no text)", style="dim")
        if event.turn_id == self._turn_id:
            self._turn_id = None
            self._tools.clear()
            self._status = StatusKind.READY
            self._status_text = ""
        self._sealed.discard(event.turn_id)
