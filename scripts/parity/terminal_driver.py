"""Transcript driver for terminal parity flows (P25-05).

Runs a command under a pseudo-terminal, keeps a small screen model of what it drew, and turns the
screen into a structured event list a comparison can read: **regions** (blocks of text separated
by blank rows), **cards** (blocks that start with a surface's card marker, e.g. an approval
request), **prompts** (rows that ask for input) and the **exit** code. ``StdioSession`` does the
same for line-delimited JSON-RPC programs (the editor protocol) without a terminal.

The screen model understands the subset of terminal control sequences line editors and full-screen
renderers use: cursor moves, erase in line/display, scroll regions, insert/delete lines, the
alternate screen and pending wrap. Colours and modes are ignored. Rows that scroll off the main
screen are kept, so text that streamed past is still searchable.
"""

from __future__ import annotations

import codecs
import contextlib
import json
import os
import re
import signal
import subprocess
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KEYS: dict[str, str] = {
    "Enter": "\r",
    "Tab": "\t",
    "Escape": "\x1b",
    "Backspace": "\x7f",
    "Up": "\x1b[A",
    "Down": "\x1b[B",
    "Right": "\x1b[C",
    "Left": "\x1b[D",
    "Home": "\x1b[H",
    "End": "\x1b[F",
    "Ctrl+C": "\x03",
    "Ctrl+D": "\x04",
    "Ctrl+L": "\x0c",
    "Space": " ",
}


def key_bytes(key: str) -> str:
    """The characters a key sends: a named key, ``Ctrl+<letter>`` or one printable character."""
    if key in KEYS:
        return KEYS[key]
    if key.startswith("Ctrl+") and len(key) == 6 and key[5].isalpha():
        return chr(ord(key[5].upper()) - 64)
    if len(key) == 1 and key.isprintable():
        return key
    raise ValueError(f"unknown key {key!r}")


# -- screen model ------------------------------------------------------------------------------


_CSI = re.compile(r"\[([\x30-\x3f]*)([\x20-\x2f]*)([\x40-\x7e])")


def _cell_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


class Screen:
    """A grid of cells fed with terminal output."""

    def __init__(self, rows: int = 24, cols: int = 80, scrollback: int = 5000) -> None:
        self.rows, self.cols, self.limit = rows, cols, scrollback
        self.grid = [self._blank() for _ in range(rows)]
        self.scrollback: list[str] = []
        self.row = self.col = 0
        self.saved = (0, 0)
        self.top, self.bottom = 0, rows - 1
        self.pending_wrap = False
        self._alt: tuple[list[list[str]], int, int] | None = None
        self._rest = ""
        self.replies: list[str] = []  # answers a real terminal sends back (cursor reports)

    def _blank(self) -> list[str]:
        return [" "] * self.cols

    # -- public --

    def feed(self, text: str) -> None:
        data = self._rest + text
        self._rest = ""
        i, n = 0, len(data)
        while i < n:
            ch = data[i]
            if ch == "\x1b":
                end = self._escape(data, i)
                if end < 0:
                    self._rest = data[i:]
                    return
                i = end
                continue
            self._char(ch)
            i += 1

    def lines(self) -> list[str]:
        """The visible rows, trailing spaces removed."""
        return ["".join(c for c in row if c).rstrip() for row in self.grid]

    def transcript(self) -> list[str]:
        """Rows that scrolled off the main screen, then the visible rows."""
        return [*self.scrollback, *self.lines()]

    # -- characters --

    def _char(self, ch: str) -> None:
        code = ord(ch)
        if ch == "\r":
            self.col, self.pending_wrap = 0, False
        elif ch in "\n\x0b\x0c":
            self._linefeed()
        elif ch == "\b":
            self.col, self.pending_wrap = max(0, self.col - 1), False
        elif ch == "\t":
            self.col = min(self.cols - 1, (self.col // 8 + 1) * 8)
        elif code < 0x20 or code == 0x7F:
            return
        else:
            self._print(ch)

    def _print(self, ch: str) -> None:
        width = _cell_width(ch)
        if width == 0:
            if self.col > 0:
                self.grid[self.row][self.col - 1] += ch
            return
        if self.pending_wrap or self.col + width > self.cols:
            self.col = 0
            self._linefeed()
            self.pending_wrap = False
        self.grid[self.row][self.col] = ch
        if width == 2 and self.col + 1 < self.cols:
            self.grid[self.row][self.col + 1] = ""
        self.col += width
        if self.col >= self.cols:
            self.col, self.pending_wrap = self.cols - 1, True

    def _linefeed(self) -> None:
        self.pending_wrap = False
        if self.row == self.bottom:
            self._scroll_up(1)
        elif self.row < self.rows - 1:
            self.row += 1

    def _scroll_up(self, count: int) -> None:
        for _ in range(count):
            gone = self.grid.pop(self.top)
            if self._alt is None and self.top == 0:
                self.scrollback.append("".join(c for c in gone if c).rstrip())
                if len(self.scrollback) > self.limit:
                    del self.scrollback[0]
            self.grid.insert(self.bottom, self._blank())

    def _scroll_down(self, count: int) -> None:
        for _ in range(count):
            del self.grid[self.bottom]
            self.grid.insert(self.top, self._blank())

    # -- escapes --

    def _escape(self, data: str, i: int) -> int:
        """Handle the escape sequence at ``data[i]``; the index after it, or -1 if incomplete."""
        if i + 1 >= len(data):
            return -1
        kind = data[i + 1]
        if kind == "[":
            match = _CSI.match(data, i + 1)
            if match is None:
                tail = data[i + 1 :]
                return -1 if re.fullmatch(r"\[[\x20-\x3f]*", tail) else i + 2
            self._csi(match.group(1), match.group(3))
            return match.end()
        if kind in "]P_^X":  # OSC / DCS / APC / PM / SOS: skip to BEL or ST
            for end in range(i + 2, len(data)):
                if data[end] == "\x07":
                    return end + 1
                if data[end] == "\x1b" and end + 1 < len(data) and data[end + 1] == "\\":
                    return end + 2
            return -1
        if kind in "()*+#%":
            return i + 3 if i + 2 < len(data) else -1
        if kind == "7":
            self.saved = (self.row, self.col)
        elif kind == "8":
            self.row, self.col = self.saved
        elif kind == "M":
            if self.row == self.top:
                self._scroll_down(1)
            else:
                self.row = max(0, self.row - 1)
        elif kind == "D":
            self._linefeed()
        elif kind == "E":
            self.col = 0
            self._linefeed()
        elif kind == "c":
            self.__init__(self.rows, self.cols, self.limit)
        return i + 2

    def _csi(self, raw: str, final: str) -> None:
        private = raw.startswith("?")
        nums = [int(p) if p.isdigit() else 0 for p in raw.lstrip("?<=>").split(";")] if raw else []

        def arg(index: int = 0, default: int = 1) -> int:
            value = nums[index] if index < len(nums) else 0
            return value or default

        self.pending_wrap = False
        if final == "n" and nums == [6]:  # cursor position report request
            self.replies.append(f"\x1b[{self.row + 1};{self.col + 1}R")
            return
        if final == "c" and not private and arg(0, 0) == 0:  # primary device attributes
            self.replies.append("\x1b[?1;2c")
            return
        if private:
            if final in "hl" and any(n in (47, 1047, 1049) for n in nums):
                self._alternate(final == "h")
            return
        if final == "A":
            self.row = max(0, self.row - arg())
        elif final == "B":
            self.row = min(self.rows - 1, self.row + arg())
        elif final == "C":
            self.col = min(self.cols - 1, self.col + arg())
        elif final == "D":
            self.col = max(0, self.col - arg())
        elif final == "E":
            self.row, self.col = min(self.rows - 1, self.row + arg()), 0
        elif final == "F":
            self.row, self.col = max(0, self.row - arg()), 0
        elif final == "G":
            self.col = min(self.cols - 1, arg() - 1)
        elif final in "Hf":
            self.row = min(self.rows - 1, arg(0) - 1)
            self.col = min(self.cols - 1, arg(1) - 1)
        elif final == "d":
            self.row = min(self.rows - 1, arg() - 1)
        elif final == "J":
            self._erase_display(nums[0] if nums else 0)
        elif final == "K":
            self._erase_line(nums[0] if nums else 0)
        elif final == "L" and self.top <= self.row <= self.bottom:
            for _ in range(arg()):
                del self.grid[self.bottom]
                self.grid.insert(self.row, self._blank())
        elif final == "M" and self.top <= self.row <= self.bottom:
            for _ in range(arg()):
                del self.grid[self.row]
                self.grid.insert(self.bottom, self._blank())
        elif final == "P":
            line = self.grid[self.row]
            count = min(arg(), self.cols - self.col)
            del line[self.col : self.col + count]
            line.extend([" "] * count)
        elif final == "@":
            line = self.grid[self.row]
            for _ in range(min(arg(), self.cols - self.col)):
                line.insert(self.col, " ")
                line.pop()
        elif final == "X":
            for c in range(self.col, min(self.cols, self.col + arg())):
                self.grid[self.row][c] = " "
        elif final == "S":
            self._scroll_up(arg())
        elif final == "T":
            self._scroll_down(arg())
        elif final == "r":
            top, bottom = arg(0) - 1, arg(1, self.rows) - 1
            if 0 <= top < bottom < self.rows:
                self.top, self.bottom = top, bottom
                self.row, self.col = 0, 0
        elif final == "s":
            self.saved = (self.row, self.col)
        elif final == "u":
            self.row, self.col = self.saved

    def _erase_line(self, mode: int) -> None:
        line = self.grid[self.row]
        start, end = {0: (self.col, self.cols), 1: (0, self.col + 1)}.get(mode, (0, self.cols))
        for c in range(start, min(end, self.cols)):
            line[c] = " "

    def _erase_display(self, mode: int) -> None:
        if mode == 0:
            self._erase_line(0)
            rows = range(self.row + 1, self.rows)
        elif mode == 1:
            self._erase_line(1)
            rows = range(0, self.row)
        else:
            rows = range(self.rows)
        for r in rows:
            self.grid[r] = self._blank()

    def _alternate(self, enter: bool) -> None:
        if enter and self._alt is None:
            self._alt = (self.grid, self.row, self.col)
            self.grid = [self._blank() for _ in range(self.rows)]
        elif not enter and self._alt is not None:
            self.grid, self.row, self.col = self._alt
            self._alt = None


# -- structure ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class TranscriptProfile:
    """What cards and prompts look like on one surface (patterns are regular expressions)."""

    cards: Mapping[str, str] = field(default_factory=dict[str, str])
    prompts: Sequence[str] = ()


def structure(lines: Sequence[str], profile: TranscriptProfile) -> dict[str, Any]:
    """Regions, cards and prompts of a screen's rows."""
    regions: list[dict[str, Any]] = []
    block: list[str] = []
    top = 0
    for index, line in enumerate([*lines, ""]):
        if line.strip():
            if not block:
                top = index
            block.append(line)
        elif block:
            regions.append({"top": top, "rows": len(block), "text": "\n".join(block)})
            block = []
    cards: list[dict[str, Any]] = []
    prompts: list[dict[str, Any]] = []
    card_patterns = {kind: re.compile(p) for kind, p in profile.cards.items()}
    prompt_patterns = [re.compile(p) for p in profile.prompts]
    for index, line in enumerate(lines):
        for kind, pattern in card_patterns.items():
            if pattern.search(line):
                cards.append({"kind": kind, "row": index, "title": line.strip()})
        if any(p.search(line) for p in prompt_patterns):
            prompts.append({"row": index, "text": line.strip()})
    return {"regions": regions, "cards": cards, "prompts": prompts}


# -- sessions ----------------------------------------------------------------------------------


class StepTimeout(Exception):
    """A wait did not see what it waited for in time."""


class _Waitable:
    def __init__(self) -> None:
        self.changed = threading.Condition()
        self.exit_code: int | None = None
        self.exited = False

    def wait_until(self, check: Callable[[], bool], timeout: float, what: str) -> None:
        deadline = time.monotonic() + timeout
        with self.changed:
            while not check():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise StepTimeout(f"timed out after {timeout:g} s waiting for {what}")
                self.changed.wait(min(left, 0.25))

    def wait_exit(self, timeout: float) -> int | None:
        self.wait_until(lambda: self.exited, timeout, "the process to exit")
        return self.exit_code


class TerminalSession(_Waitable):
    """One command in a pseudo-terminal, with a screen model fed by a reader thread."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
        rows: int = 40,
        cols: int = 110,
        profile: TranscriptProfile | None = None,
    ) -> None:
        super().__init__()
        from ptyprocess import PtyProcess  # dev dependency (through pexpect)

        self.profile = profile or TranscriptProfile()
        self.screen = Screen(rows, cols)
        self.raw = bytearray()
        self.proc: Any = PtyProcess.spawn(
            list(argv), cwd=str(cwd), env=dict(env), dimensions=(rows, cols)
        )
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._reader = threading.Thread(target=self._read, name="parity-pty", daemon=True)
        self._reader.start()

    def _read(self) -> None:
        while True:
            try:
                data: bytes = self.proc.read(65536)
            except EOFError:
                break
            except OSError:
                break
            with self.changed:
                self.raw.extend(data)
                self.screen.feed(self._decoder.decode(data))
                replies, self.screen.replies = self.screen.replies, []
                self.changed.notify_all()
            for reply in replies:
                with contextlib.suppress(OSError):
                    self.proc.write(reply.encode("ascii"))
        code: int | None
        try:
            self.proc.wait()
            code = self.proc.exitstatus
            if code is None and self.proc.signalstatus is not None:
                code = 128 + int(self.proc.signalstatus)
        except Exception:  # noqa: BLE001 - the child is already gone
            code = None
        with self.changed:
            self.exit_code, self.exited = code, True
            self.changed.notify_all()

    def send(self, text: str) -> None:
        self.proc.write(text.encode("utf-8"))

    def press(self, key: str) -> None:
        self.send(key_bytes(key))

    def text(self, scope: str = "transcript") -> str:
        with self.changed:
            rows = self.screen.lines() if scope == "screen" else self.screen.transcript()
        return "\n".join(rows)

    def snapshot(self) -> dict[str, Any]:
        with self.changed:
            rows = self.screen.lines()
        while rows and not rows[-1]:
            rows.pop()
        return {"screen": rows, **structure(rows, self.profile)}

    def close(self) -> None:
        """Stop the child and everything in its process group (ours alone)."""
        if not self.exited:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            self.proc.close(force=True)
        self._reader.join(5)


class StdioSession(_Waitable):
    """A program that speaks newline-delimited JSON on stdin/stdout (JSON-RPC 2.0)."""

    def __init__(self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path) -> None:
        super().__init__()
        self.messages: list[dict[str, Any]] = []
        self.stderr_tail: list[str] = []
        self.proc = subprocess.Popen(  # noqa: S603 - argv from the runner's own table
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self._threads = [
            threading.Thread(target=self._read_out, daemon=True),
            threading.Thread(target=self._read_err, daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _read_out(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            try:
                message = json.loads(raw)
            except ValueError:
                message = {"unparsed": raw.decode("utf-8", "replace").rstrip()}
            with self.changed:
                self.messages.append(message if isinstance(message, dict) else {"value": message})
                self.changed.notify_all()
        code = self.proc.wait()
        with self.changed:
            self.exit_code, self.exited = code, True
            self.changed.notify_all()

    def _read_err(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            self.stderr_tail = [*self.stderr_tail[-40:], raw.decode("utf-8", "replace").rstrip()]

    def send(self, message: Mapping[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def close_input(self) -> None:
        if self.proc.stdin is not None:
            with contextlib.suppress(OSError):
                self.proc.stdin.close()

    def find(self, match: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
        with self.changed:
            return next((m for m in self.messages if match(m)), None)

    def close(self) -> None:
        self.close_input()
        if self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(5)
        for thread in self._threads:
            thread.join(5)


__all__ = [
    "KEYS",
    "Screen",
    "StdioSession",
    "StepTimeout",
    "TerminalSession",
    "TranscriptProfile",
    "key_bytes",
    "structure",
]
