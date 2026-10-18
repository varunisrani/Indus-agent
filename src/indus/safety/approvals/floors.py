# pyright: strict
"""Floors: commands no switch can let through (P05-08, R02.4).

The pipeline checks these before auto-approve, ``approvals.mode: off``, an unattended approve
policy or the permanent allowlist:

- the hardline set from detection (``Detection.hardline``), which also covers commands that
  cannot be parsed (``malformed``) or are too large to check (``parser_limit``): fail closed;
- deleting the interpreter or virtual environment Indus itself runs from (the next start would
  fail before any tool could repair it);
- ``sudo -S`` / ``sudo --stdin`` written by the agent while no sudo password is configured
  (password guessing; Indus feeds a configured password itself, P05-10);
- the user's ``approvals.deny`` globs, matched case-insensitively against several spellings of
  the command so quoting and wrappers do not dodge them.

Each check is a plain function; ``FloorHit`` carries the key, a description and the text for
the model.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass

from indus.safety.approvals.detect import Detection, normalise
from indus.safety.approvals.shlex_lite import (
    ShellLimitError,
    ShellSyntaxError,
    SimpleCommand,
    Word,
    parse,
)

__all__ = [
    "FloorHit",
    "deny_variants",
    "hardline_hit",
    "match_deny_glob",
    "runtime_delete_target",
    "runtime_roots",
    "sudo_stdin_guess",
]

MAX_NESTING = 4

_DELETERS = frozenset({"rm", "rmdir", "unlink", "shred", "srm", "trash", "trash-put"})
_SUDO_VALUES = frozenset(
    {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T"}
    | {"--user", "--group", "--host", "--prompt", "--close-from", "--chdir", "--role", "--type"}
    | {"--other-user", "--command-timeout"}
)
# Wrapper → its options that take a separate value.
_WRAPPERS: dict[str, frozenset[str]] = {
    "sudo": _SUDO_VALUES,
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-a", "--argv0"}),
    "exec": frozenset({"-a"}),
    "nohup": frozenset(),
    "setsid": frozenset(),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "command": frozenset(),
    "builtin": frozenset(),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "--class", "-n", "--classdata"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "unbuffer": frozenset(),
    "timeout": frozenset({"-k", "--kill-after", "-s", "--signal"}),
    "chrt": frozenset(),
    "taskset": frozenset(),
}
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "mksh", "fish"})
_KEYWORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}"})
_GLOB_CHARS = frozenset("*?[")


@dataclass(frozen=True)
class FloorHit:
    key: str
    description: str
    message: str


# -- helpers ---------------------------------------------------------------------------------------


def _parse(text: str) -> list[SimpleCommand] | None:
    try:
        return parse(text)
    except (ShellSyntaxError, ShellLimitError):
        return None


def _base(word: str) -> str:
    return word.rsplit("/", 1)[-1].lower()


def _is_assignment(text: str) -> bool:
    name, eq, _ = text.partition("=")
    return bool(eq) and name.replace("_", "a").isalnum() and not name[:1].isdigit()


def _strip_wrappers(words: Sequence[Word]) -> list[Word]:
    """Drop leading assignments, shell keywords and wrapper words with their options.

    Approximate on purpose (attached option values, ``env -S`` and the like are not followed):
    it only gives the deny globs and the delete check one more spelling of the command.
    """
    rest = list(words)
    while rest:
        text = rest[0].text
        if _is_assignment(text) or text in _KEYWORDS:
            rest = rest[1:]
            continue
        values = _WRAPPERS.get(_base(text))
        if values is not None:
            rest = rest[1:]
            while rest and rest[0].text.startswith("-") and rest[0].text != "--":
                option = rest[0].text
                rest = rest[2:] if option in values else rest[1:]
            if rest and rest[0].text == "--":
                rest = rest[1:]
            if rest and rest[0].text.rstrip("smhd").replace(".", "").isdigit():  # timeout 5s
                rest = rest[1:]
            continue
        break
    return rest


def _runs_next_word(previous: str) -> bool:
    """``-c``, ``-lc``, ``-e``, ``--command``, ``--eval``: the next word is code."""
    if previous in ("--command", "--eval"):
        return True
    return (
        previous.startswith("-")
        and not previous.startswith("--")
        and previous[-1:] in ("c", "e")
        and previous[1:].isalpha()
    )


def _nested_texts(command: SimpleCommand) -> Iterator[str]:
    """Texts that run as commands of their own: substitutions, ``-c``/``-e`` payloads and
    ``eval`` arguments (a quoted commit message is not one)."""
    words = command.words
    evaluates = bool(words) and _base(words[0].text) == "eval"
    for index, word in enumerate(words):
        for sub in word.subs:
            yield sub.text
        if index and (evaluates or _runs_next_word(words[index - 1].text)):
            yield word.text
    stripped = _strip_wrappers(words)
    shell = bool(stripped) and _base(stripped[0].text) in _SHELLS
    for redirect in command.redirects:
        for sub in redirect.target.subs:
            yield sub.text
        if redirect.body and shell:
            yield redirect.body


def _walk(text: str, depth: int = 0) -> Iterator[SimpleCommand]:
    """Every simple command of ``text``, nested payloads included (up to ``MAX_NESTING``)."""
    commands = _parse(text)
    if commands is None:
        return
    for command in commands:
        yield command
        if depth < MAX_NESTING:
            for nested in _nested_texts(command):
                yield from _walk(nested, depth + 1)


# -- hardline --------------------------------------------------------------------------------------


def hardline_hit(detection: Detection) -> FloorHit | None:
    if not detection.hardline:
        return None
    key = detection.pattern_key or "hardline"
    description = detection.description or "a command that is never run"
    message = (
        f"BLOCKED (never runs): {description}. This command is refused whatever the approval "
        "settings (auto-approve, approvals.mode off, cron approve mode or the allowlist). "
        "Do not retry or rephrase it. If it is really needed, the user can run it in their own "
        "terminal."
    )
    if key in ("malformed", "parser_limit"):
        message += (
            " This block is about the command's shape, not what it does: write the script to a "
            "file with write_file and run that file instead of inlining it."
        )
    return FloorHit(key, description, message)


# -- interpreter / venv delete ---------------------------------------------------------------------


def runtime_roots() -> tuple[str, ...]:
    """The places Indus runs from: the interpreter's prefix (the venv) and the interpreter."""
    roots: list[str] = []
    for path in (sys.prefix, sys.executable):
        if path:
            roots.append(os.path.realpath(path))
            roots.append(os.path.normpath(path))
    return tuple(dict.fromkeys(roots))


def _ancestors(path: str) -> Iterator[str]:
    current = path
    while True:
        yield current
        parent = posixpath.dirname(current)
        if parent == current:
            return
        current = parent


def _expand(operand: str, *, home: str | None, cwd: str | None) -> str | None:
    text = operand
    if text == "~" or text.startswith("~/"):
        if not home:
            return None
        text = home + text[1:]
    if not text.startswith("/"):
        if not cwd:
            return None
        text = posixpath.join(cwd, text)
    return posixpath.normpath(text)


def _hits_root(target: str, roots: Iterable[str]) -> str | None:
    """The root ``target`` would delete: the root itself, something above it, or (for a glob)
    a path the glob matches on the way up to it."""
    globbing = any(ch in target for ch in _GLOB_CHARS)
    for root in roots:
        for path in _ancestors(root):
            if path == "/":
                break
            if target == path or (globbing and fnmatch.fnmatchcase(path, target)):
                return root
    return None


def _delete_operands(command: SimpleCommand) -> list[Word]:
    words = _strip_wrappers(command.words)
    if not words:
        return []
    program = _base(words[0].text)
    if program in _DELETERS:
        operands: list[Word] = []
        after_dashes = False
        for word in words[1:]:
            if not after_dashes and word.text == "--":
                after_dashes = True
                continue
            if not after_dashes and word.text.startswith("-") and len(word.text) > 1:
                continue
            operands.append(word)
        return operands
    if program == "find" and any(w.text in ("-delete", "-exec", "-execdir") for w in words):
        roots: list[Word] = []
        for word in words[1:]:
            if word.text.startswith(("-", "(", "!")):
                break
            roots.append(word)
        return roots
    return []


def runtime_delete_target(
    command: str,
    *,
    roots: Sequence[str] | None = None,
    home: str | None = None,
    cwd: str | None = None,
) -> str | None:
    """The runtime path ``command`` would delete, or None. Relative operands are resolved only
    when ``cwd`` is given; ``~`` only when ``home`` is."""
    chosen = tuple(roots) if roots is not None else runtime_roots()
    for simple in _walk(normalise(command)):
        for operand in _delete_operands(simple):
            target = _expand(operand.text, home=home, cwd=cwd)
            if target is None:
                continue
            hit = _hits_root(target, chosen)
            if hit is not None:
                return hit
    return None


# -- sudo -S guessing ------------------------------------------------------------------------------


def _sudo_reads_stdin(words: Sequence[Word]) -> bool:
    index = 0
    while index < len(words):
        text = words[index].text
        if text == "--" or not text.startswith("-"):
            return False
        if text == "--stdin":
            return True
        if text.startswith("--"):
            index += 2 if text in _SUDO_VALUES else 1
            continue
        letters = text[1:]
        for position, letter in enumerate(letters):
            if letter == "S":
                return True
            if f"-{letter}" in _SUDO_VALUES:
                # the rest of the word (or the next word) is this option's value
                index += 1 if position + 1 < len(letters) else 2
                break
        else:
            index += 1
    return False


def sudo_stdin_guess(command: str) -> bool:
    """True when the command runs ``sudo`` with ``-S``/``--stdin`` anywhere (nested payloads
    included): the agent is piping a password it made up or read somewhere."""
    for simple in _walk(normalise(command)):
        words = simple.words
        for index, word in enumerate(words):
            if _base(word.text) == "sudo" and _sudo_reads_stdin(words[index + 1 :]):
                return True
    return False


# -- deny globs ------------------------------------------------------------------------------------


def deny_variants(command: str) -> list[str]:
    """Spellings of ``command`` the deny globs are matched against (lower-cased)."""
    seen: dict[str, None] = {}

    def add(text: str) -> None:
        cleaned = " ".join(text.split()).lower()
        if cleaned:
            seen.setdefault(cleaned, None)

    add(command)
    normal = normalise(command)
    add(normal)
    for simple in _walk(normal):
        if not simple.words:
            continue
        add(" ".join(w.text for w in simple.words))
        stripped = _strip_wrappers(simple.words)
        if stripped:
            add(" ".join(w.text for w in stripped))
    return list(seen)


def match_deny_glob(command: str, globs: Iterable[object]) -> str | None:
    """The first ``approvals.deny`` glob that matches any spelling of ``command``."""
    patterns = [g.strip() for g in globs if isinstance(g, str) and g.strip()]
    if not patterns:
        return None
    variants = deny_variants(command)
    for pattern in patterns:
        lowered = " ".join(pattern.split()).lower()
        for variant in variants:
            if fnmatch.fnmatchcase(variant, lowered):
                return pattern
    return None
