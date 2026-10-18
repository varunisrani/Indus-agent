# pyright: strict
"""A small POSIX-shell lexer for command analysis (not for running commands).

``parse(text)`` splits a command line into simple commands. Each has its words with quotes removed
and escapes applied, its redirections (here-document bodies attached) and the operator that ended
it (``|``, ``||``, ``&&``, ``;``, ``&``, a newline, ``(`` / ``)``, or ``""`` at the end). Command
and process substitutions are not expanded: a word keeps their source text and lists them in
``subs`` so the caller can analyse them as scripts of their own.

Anything bash itself would reject as unterminated (a quote, ``$(``, ``${``, a backtick) raises
``ShellSyntaxError``; too much input raises ``ShellLimitError``. Both mean "do not trust this".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

BLANKS = " \t\r"
_OPERATORS = ("||", "|&", "&&", ";;&", ";;", ";&", "|", "&", ";", "(", ")")
_REDIRECTS = ("<<<", "<<-", "<<", "<>", "<&", ">>", ">|", ">&", "&>>", "&>", "<", ">")
_WORD_BREAK = set(BLANKS) | set("\n|&;()<>")
_BRACE_EXPANSION = re.compile(r"\{[^{}\s]*(?:,|\.\.)[^{}\s]*\}")
_ANSI_C = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}


class ShellSyntaxError(ValueError):
    """The text is not a complete shell command."""


class ShellLimitError(ValueError):
    """The text is too large to analyse."""


@dataclass
class Sub:
    """A command substitution (``$(``, a backtick) or process substitution (``<(``, ``>(``)."""

    kind: str
    text: str


@dataclass
class Word:
    text: str
    quoted: bool = False
    dynamic: bool = False  # unquoted glob or brace-expansion characters
    subs: list[Sub] = field(default_factory=list[Sub])


@dataclass
class Redirect:
    op: str
    target: Word
    fd: str = ""
    body: str | None = None  # here-document text
    strip_tabs: bool = False


@dataclass
class SimpleCommand:
    words: list[Word] = field(default_factory=list[Word])
    redirects: list[Redirect] = field(default_factory=list[Redirect])
    op: str = ""


def _match_close(s: str, start: int, opener: str, closer: str) -> int:
    """Index of the ``closer`` that balances an already-consumed ``opener`` before ``start``."""
    depth = 1
    i = start
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise ShellSyntaxError("unterminated single quote")
            i = j + 1
            continue
        if c == '"':
            i = _skip_dquote(s, i + 1)
            continue
        if c == "`":
            i = _find_backtick(s, i + 1) + 1
            continue
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ShellSyntaxError(f"unterminated {opener}…{closer}")


def _skip_dquote(s: str, start: int) -> int:
    """Index just past the double quote that closes a string opened before ``start``."""
    i = start
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        if c == "$" and s.startswith("$(", i):
            i = _match_close(s, i + 2, "(", ")") + 1
            continue
        if c == "$" and s.startswith("${", i):
            i = _match_close(s, i + 2, "{", "}") + 1
            continue
        if c == "`":
            i = _find_backtick(s, i + 1) + 1
            continue
        i += 1
    raise ShellSyntaxError("unterminated double quote")


def _find_backtick(s: str, start: int) -> int:
    i = start
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "`":
            return i
        i += 1
    raise ShellSyntaxError("unterminated backtick")


def _ansi_c(s: str, start: int) -> tuple[str, int]:
    """Decode ``$'…'`` starting after the opening quote; return (text, index past the close)."""
    out: list[str] = []
    i = start
    n = len(s)
    while i < n:
        c = s[i]
        if c == "'":
            return "".join(out), i + 1
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        nxt = s[i + 1]
        if nxt in _ANSI_C:
            out.append(_ANSI_C[nxt])
            i += 2
        elif nxt == "x":
            digits = re.match(r"[0-9A-Fa-f]{1,2}", s[i + 2 :])
            if digits:
                out.append(chr(int(digits.group(0), 16)))
                i += 2 + len(digits.group(0))
            else:
                out.append("\\x")
                i += 2
        elif nxt in "01234567":
            digits = re.match(r"[0-7]{1,3}", s[i + 1 :])
            assert digits is not None
            out.append(chr(int(digits.group(0), 8) & 0xFF))
            i += 1 + len(digits.group(0))
        else:
            out.append("\\" + nxt)
            i += 2
    raise ShellSyntaxError("unterminated $'…' quote")


class _Lexer:
    def __init__(self, text: str, max_words: int) -> None:
        self.s: str = text
        self.i: int = 0
        self.n: int = len(text)
        self.max_words: int = max_words
        self.words: int = 0
        self.pending: list[Redirect] = []

    # -- words ---------------------------------------------------------------------------------

    def word(self) -> Word:
        s = self.s
        text: list[str] = []
        bare: list[str] = []
        subs: list[Sub] = []
        quoted = False
        while self.i < self.n:
            c = s[self.i]
            if c in "<>" and s.startswith("(", self.i + 1):
                close = _match_close(s, self.i + 2, "(", ")")
                subs.append(Sub(c + "(", s[self.i + 2 : close]))
                text.append(s[self.i : close + 1])
                self.i = close + 1
                continue
            if c in _WORD_BREAK:
                break
            if c == "\\":
                if s.startswith("\n", self.i + 1):
                    self.i += 2
                    continue
                if self.i + 1 < self.n:
                    text.append(s[self.i + 1])
                    quoted = True
                self.i += 2
                continue
            if c == "'":
                close = s.find("'", self.i + 1)
                if close < 0:
                    raise ShellSyntaxError("unterminated single quote")
                text.append(s[self.i + 1 : close])
                quoted = True
                self.i = close + 1
                continue
            if c == '"':
                self.i = self.dquote(text, subs)
                quoted = True
                continue
            if c == "$":
                self.dollar(text, subs, quote_context=False)
                continue
            if c == "`":
                close = _find_backtick(s, self.i + 1)
                subs.append(Sub("`", s[self.i + 1 : close].replace("\\`", "`")))
                text.append(s[self.i : close + 1])
                self.i = close + 1
                continue
            text.append(c)
            bare.append(c)
            self.i += 1
        self.words += 1
        if self.words > self.max_words:
            raise ShellLimitError("too many words")
        plain = "".join(bare)
        dynamic = any(ch in plain for ch in "*?[") or bool(_BRACE_EXPANSION.search(plain))
        return Word("".join(text), quoted=quoted, dynamic=dynamic, subs=subs)

    def dquote(self, text: list[str], subs: list[Sub]) -> int:
        s = self.s
        self.i += 1
        while self.i < self.n:
            c = s[self.i]
            if c == '"':
                return self.i + 1
            if c == "\\" and self.i + 1 < self.n:
                nxt = s[self.i + 1]
                if nxt in '$`"\\':
                    text.append(nxt)
                elif nxt != "\n":
                    text.append("\\" + nxt)
                self.i += 2
                continue
            if c == "$":
                self.dollar(text, subs, quote_context=True)
                continue
            if c == "`":
                close = _find_backtick(s, self.i + 1)
                subs.append(Sub("`", s[self.i + 1 : close].replace("\\`", "`")))
                text.append(s[self.i : close + 1])
                self.i = close + 1
                continue
            text.append(c)
            self.i += 1
        raise ShellSyntaxError("unterminated double quote")

    def dollar(self, text: list[str], subs: list[Sub], *, quote_context: bool) -> None:
        s = self.s
        i = self.i
        if not quote_context and s.startswith("$'", i):
            decoded, end = _ansi_c(s, i + 2)
            text.append(decoded)
            self.i = end
            return
        if s.startswith("$((", i):
            close = _match_close(s, i + 3, "(", ")")
            # ``$(( … ))`` closes with two parens; ``$( ( … ) )`` is a subshell substitution.
            if s.startswith(")", close + 1):
                text.append(s[i : close + 2])
                self.i = close + 2
                return
        if s.startswith("$(", i):
            close = _match_close(s, i + 2, "(", ")")
            subs.append(Sub("$(", s[i + 2 : close]))
            text.append(s[i : close + 1])
            self.i = close + 1
            return
        if s.startswith("${", i):
            close = _match_close(s, i + 2, "{", "}")
            text.append(s[i : close + 1])
            self.i = close + 1
            return
        text.append("$")
        self.i = i + 1

    # -- the command list ----------------------------------------------------------------------

    def redirect(self, command: SimpleCommand) -> None:
        s = self.s
        start = self.i
        while self.i < self.n and s[self.i].isdigit():
            self.i += 1
        fd = s[start : self.i]
        op = next(o for o in _REDIRECTS if s.startswith(o, self.i))
        self.i += len(op)
        while self.i < self.n and s[self.i] in BLANKS:
            self.i += 1
        process_sub = s[self.i : self.i + 1] in ("<", ">") and s.startswith("(", self.i + 1)
        if self.i >= self.n or (s[self.i] in "\n|&;()<>" and not process_sub):
            raise ShellSyntaxError(f"redirection {op} has no target")
        target = self.word()
        redirect = Redirect(op, target, fd=fd, strip_tabs=op == "<<-")
        if op in ("<<", "<<-"):
            self.pending.append(redirect)
        command.redirects.append(redirect)

    def heredoc_bodies(self) -> None:
        for redirect in self.pending:
            delimiter = redirect.target.text
            lines: list[str] = []
            while self.i < self.n:
                end = self.s.find("\n", self.i)
                line = self.s[self.i : end if end >= 0 else self.n]
                self.i = end + 1 if end >= 0 else self.n
                check = line.lstrip("\t") if redirect.strip_tabs else line
                if check == delimiter:
                    break
                lines.append(check)
            redirect.body = "\n".join(lines)
        self.pending = []

    def at_redirect(self) -> bool:
        s = self.s
        j = self.i
        while j < self.n and s[j].isdigit():
            j += 1
        if j < self.n and s[j] in "<>":
            return not s.startswith("(", j + 1)
        return s.startswith("&>", self.i)

    def parse(self) -> list[SimpleCommand]:
        s = self.s
        commands: list[SimpleCommand] = []
        current = SimpleCommand()

        def finish(op: str) -> None:
            nonlocal current
            current.op = op
            if current.words or current.redirects or op in ("(", ")"):
                commands.append(current)
            current = SimpleCommand()

        while self.i < self.n:
            c = s[self.i]
            if c in BLANKS:
                self.i += 1
            elif c == "\\" and s.startswith("\n", self.i + 1):
                self.i += 2
            elif c == "#":
                end = s.find("\n", self.i)
                self.i = end if end >= 0 else self.n
            elif c == "\n":
                finish("\n")
                self.i += 1
                self.heredoc_bodies()
            elif self.at_redirect():
                self.redirect(current)
            elif c in "|&;()":
                op = next(o for o in _OPERATORS if s.startswith(o, self.i))
                finish(op)
                self.i += len(op)
            else:
                current.words.append(self.word())
        finish("")
        self.heredoc_bodies()
        return commands


def parse(text: str, *, max_words: int = 20_000) -> list[SimpleCommand]:
    """Simple commands of ``text`` in order; see the module docstring."""
    return _Lexer(text, max_words).parse()


def pipelines(commands: list[SimpleCommand]) -> list[list[SimpleCommand]]:
    """Group commands joined by ``|`` / ``|&`` into pipelines."""
    groups: list[list[SimpleCommand]] = []
    current: list[SimpleCommand] = []
    for command in commands:
        if command.words or command.redirects:
            current.append(command)
        if command.op not in ("|", "|&"):
            if current:
                groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups
