"""A small PowerShell lexer for the installer's static tests (no PowerShell on this host).

It splits a script into code and the parts the parser treats as text (comments, single- and
double-quoted strings, here-strings), which is enough to check bracket balance, which functions are
called and defined, and which syntax is used, without running anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "Lexed",
    "function_names",
    "lex",
    "script_vars_read",
    "script_vars_set",
    "verb_noun_calls",
]


@dataclass
class Lexed:
    code: str
    """The script with every comment and string body replaced by spaces (newlines kept)."""
    double_quoted: list[str] = field(default_factory=list)
    single_quoted: list[str] = field(default_factory=list)
    here_strings: list[str] = field(default_factory=list)


def _blank(text: str) -> str:
    return "".join("\n" if ch == "\n" else " " for ch in text)


def lex(source: str) -> Lexed:  # noqa: C901 - one state machine reads best in one place
    out: list[str] = []
    result = Lexed(code="")
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if ch == "<" and nxt == "#":
            end = source.find("#>", i + 2)
            end = n if end < 0 else end + 2
            out.append(_blank(source[i:end]))
            i = end
            continue
        if ch == "#":
            end = source.find("\n", i)
            end = n if end < 0 else end
            out.append(_blank(source[i:end]))
            i = end
            continue
        if ch == "@" and nxt in ("'", '"') and source[i + 2 : i + 3] == "\n":
            closer = "\n" + nxt + "@"
            end = source.find(closer, i + 3)
            if end < 0:
                raise ValueError(f"unterminated here-string at offset {i}")
            result.here_strings.append(source[i + 3 : end])
            out.append("@" + nxt + _blank(source[i + 2 : end + 1]) + nxt + "@")
            i = end + 3
            continue
        if ch == "'":
            j = i + 1
            while j < n:
                if source[j] == "'":
                    if j + 1 < n and source[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                raise ValueError(f"unterminated single-quoted string at offset {i}")
            result.single_quoted.append(source[i + 1 : j])
            out.append("'" + _blank(source[i + 1 : j]) + "'")
            i = j + 1
            continue
        if ch == '"':
            j = i + 1
            depth = 0
            while j < n:
                c = source[j]
                if c == "`":
                    j += 2
                    continue
                if c == "$" and j + 1 < n and source[j + 1] == "(":
                    depth += 1
                    j += 2
                    continue
                if depth and c == "(":
                    depth += 1
                elif depth and c == ")":
                    depth -= 1
                elif depth and c == "'":
                    close = source.find("'", j + 1)
                    j = close if close > 0 else j
                elif c == '"' and depth == 0:
                    break
                j += 1
            if j >= n:
                raise ValueError(f"unterminated double-quoted string at offset {i}")
            result.double_quoted.append(source[i + 1 : j])
            out.append('"' + _blank(source[i + 1 : j]) + '"')
            i = j + 1
            continue
        out.append(ch)
        i += 1
    result.code = "".join(out)
    return result


_CALL = re.compile(r"(?<![\w$-])([A-Z][A-Za-z]+-Indus[A-Za-z0-9]*)\b")
_DEF = re.compile(r"^\s*function\s+([A-Za-z]+-[A-Za-z0-9]+)", re.MULTILINE)
_SCRIPT_READ = re.compile(r"\$script:(Indus[A-Za-z0-9]*)")
_SCRIPT_SET = re.compile(r"\$script:(Indus[A-Za-z0-9]*)(?:\.[A-Za-z]+)?\s*=(?!=)")


def verb_noun_calls(code: str) -> set[str]:
    return set(_CALL.findall(code))


def function_names(code: str) -> set[str]:
    return set(_DEF.findall(code))


def script_vars_read(source: str) -> set[str]:
    return set(_SCRIPT_READ.findall(source))


def script_vars_set(source: str) -> set[str]:
    return set(_SCRIPT_SET.findall(source))
