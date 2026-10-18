# pyright: strict
"""Masking secrets in tool output: the P01 redactor plus command awareness (P05-06).

``redact_output`` is what every tool result, spill file, receipt and overflow log passes through.
It masks credential shapes and the exact secret values the scope holds (``foundation.redact``) and
adds secret-bearing request headers and chat-bot tokens. The ``NAME=value`` pass runs only where
the output is a credential dump: an env dump (``env``, ``printenv``, ``export -p``, ``set``,
``declare``), a read of a secret-bearing file (``.env``, shell rc files, credential dotfiles,
Indus's own config, auth and secret files), or output whose source is unknown. Source code and
ordinary config keep their ``max_token = 4096`` lines, except diagnostic lines (pytest's
``E   …`` lines and a final ``…Error: …`` line), which print values. ``force=True`` masks even
when ``security.redact_secrets`` is off.

Redaction never raises: when masking fails the text is withheld behind a marker.
"""

from __future__ import annotations

import logging
import re
import shlex
import threading
import weakref
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from indus.foundation.redact import Redactor, mask, redact_text
from indus.foundation.scope import Scope, scope_or_none

log = logging.getLogger(__name__)

WITHHELD = "[output withheld: redaction failed]"

ENV_DUMP_PROGRAMS = frozenset({"env", "printenv", "set", "export", "declare", "typeset"})
FILE_READERS = frozenset(
    {
        "cat",
        "bat",
        "batcat",
        "head",
        "tail",
        "less",
        "more",
        "nl",
        "tac",
        "zcat",
        "view",
        "strings",
        "xxd",
        "od",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "awk",
        "sed",
    }
)
# These take a pattern or program as their first operand; only later operands are files.
PATTERN_FIRST = frozenset({"grep", "egrep", "fgrep", "rg", "awk", "sed"})
_PREFIX_WORDS = frozenset({"sudo", "command", "builtin", "exec", "nohup", "time", "doas"})

SHELL_RC_FILES = frozenset(
    {
        ".bashrc",
        ".bash_profile",
        ".bash_login",
        ".bash_logout",
        ".profile",
        ".zshrc",
        ".zprofile",
        ".zshenv",
        ".zlogin",
        ".kshrc",
        ".envrc",
    }
)
CREDENTIAL_FILES = frozenset(
    {".netrc", ".pgpass", ".npmrc", ".pypirc", ".git-credentials", ".dockercfg", "credentials"}
)
INDUS_SECRET_FILES = frozenset({"config.yaml", "auth.json", "secrets.json", ".env"})

_ASSIGNMENT_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_HEADER_NAMES = (
    "x-api-key|api-key|apikey|x-api-token|x-auth-token|x-access-token|x-goog-api-key|"
    "private-token|x-vault-token|x-amz-security-token|cookie|set-cookie"
)
# A chat-bot token: numeric bot id, a colon, then a long opaque secret.
_BOT_TOKEN = re.compile(
    r"(?:(?<=bot)|(?<![A-Za-z0-9]))\d{6,12}:(?P<s>[A-Za-z0-9_-]{30,64})(?![A-Za-z0-9_-])"
)
# Diagnostic lines (pytest's "E   " lines, a final "SomeError: …" line) that print values in
# source-shaped output: the assignment pass runs on these lines even in code mode.
_DIAGNOSTIC_LINE = re.compile(r"(?m)^(?:[ \t]*E[ \t]{2,}.*|[A-Za-z_][\w.]*(?:Error|Exception):.*)$")
_SECRET_HEADER = re.compile(
    r"(?im)(?<![A-Za-z0-9_-])(?:" + _HEADER_NAMES + r")[ \t]*:[ \t]*[\"']?(?P<s>[^\s\"',;]{4,4096})"
)


# -- command classification ----------------------------------------------------------------------


def split_segments(command: str) -> list[str]:
    """Pipeline and list members of ``command``: split on unquoted ``| ; &`` and newlines.

    Backslashes are kept literally (a Windows path is not an escape here); a quote that never
    closes swallows the rest, which only makes the classification more conservative.
    """
    segments: list[str] = []
    current: list[str] = []
    quote = ""
    for ch in command:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
            current.append(ch)
        elif ch in "|;&\n":
            segments.append("".join(current))
            current = []
        else:
            current.append(ch)
    segments.append("".join(current))
    return [s.strip() for s in segments if s.strip()]


def _words(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=True)
    except ValueError:
        return segment.split()


def _program_and_args(segment: str) -> tuple[str, list[str]]:
    words = _words(segment)
    while words and (_ASSIGNMENT_WORD.match(words[0]) or words[0] in _PREFIX_WORDS):
        if words[0] in ("sudo", "doas"):
            words = words[1:]
            while words and words[0].startswith("-"):
                takes_value = words[0] in ("-u", "-g", "-C", "-h", "-p")
                words = words[2:] if takes_value else words[1:]
            continue
        words = words[1:]
    if not words:
        return "", []
    return PurePosixPath(words[0]).name.lower(), words[1:]


def _is_dump(program: str, args: list[str]) -> bool:
    if program not in ENV_DUMP_PROGRAMS:
        return False
    if program == "printenv":
        return True
    if program == "env":
        # ``env`` dumps unless it is running a command: every argument is an option or NAME=value.
        return all(a.startswith("-") or _ASSIGNMENT_WORD.match(a) for a in args)
    if program == "set":
        return not args or args == ["-o", "posix"]
    # export / declare / typeset: a listing with no names, or with the print flag.
    if not args:
        return True
    listing = any(a in ("-p", "-x", "-px", "-xp") for a in args)
    return listing and not any(_ASSIGNMENT_WORD.match(a) for a in args)


def is_env_dump(command: str | None) -> bool:
    """True when some segment of ``command`` prints environment variables."""
    if not command:
        return False
    return any(_is_dump(*_program_and_args(seg)) for seg in split_segments(command))


def _is_secret_path(arg: str, indus_home: Path | None) -> bool:
    text = arg.strip("\"'").replace("\\", "/")
    if not text or text.startswith("-"):
        return False
    parts = [p for p in text.split("/") if p]
    if not parts:
        return False
    base = parts[-1].lower()
    if base == ".env" or base.startswith(".env.") or base.endswith(".env"):
        return True
    if base in SHELL_RC_FILES:
        return True
    if base in CREDENTIAL_FILES:
        return base != "credentials" or (len(parts) > 1 and parts[-2] in (".aws", ".config"))
    if base in INDUS_SECRET_FILES:
        if text.startswith(("$INDUS_HOME/", "${INDUS_HOME}/")) or ".indus" in parts[:-1]:
            return True
        if indus_home is not None:
            try:
                return Path(text).expanduser().resolve().is_relative_to(indus_home.resolve())
            except (OSError, ValueError):
                return False
    return False


def reads_secret_file(command: str | None, *, indus_home: Path | None = None) -> bool:
    """True when some segment of ``command`` prints a secret-bearing file."""
    if not command:
        return False
    for segment in split_segments(command):
        program, args = _program_and_args(segment)
        if program not in FILE_READERS:
            continue
        operands = [a for a in args if not a.startswith("-")]
        if program in PATTERN_FIRST and not any(a in ("-e", "-f") for a in args):
            operands = operands[1:]
        if any(_is_secret_path(a, indus_home) for a in operands):
            return True
    # ``< .env`` redirections feed a file to any program.
    return any(_is_secret_path(m, indus_home) for m in re.findall(r"<\s*(\S+)", command))


# -- masking --------------------------------------------------------------------------------------

_lock = threading.Lock()
_redactors: dict[int, tuple[weakref.ref[Scope], Redactor]] = {}
_MAX_CACHED = 64


def _redactor_for(scope: Scope) -> Redactor:
    key = id(scope)
    with _lock:
        cached = _redactors.get(key)
        if cached is not None and cached[0]() is scope:
            return cached[1]
        if len(_redactors) >= _MAX_CACHED:
            _redactors.clear()
        redactor = Redactor(scope)
        _redactors[key] = (weakref.ref(scope), redactor)
        return redactor


def _mask_group(match: re.Match[str]) -> str:
    base = match.start()
    start, end = match.span("s")
    whole = match.group(0)
    return whole[: start - base] + mask(match.group("s")) + whole[end - base :]


def _mask_extra_shapes(text: str) -> str:
    """Shapes P01 does not cover: secret request headers and chat-bot tokens."""
    return _BOT_TOKEN.sub(_mask_group, _SECRET_HEADER.sub(_mask_group, text))


def _diagnostics(text: str, redact: Callable[[str], str]) -> str:
    """Run ``redact`` (with the assignment pass) over diagnostic lines only."""
    return _DIAGNOSTIC_LINE.sub(lambda m: redact(m.group(0)), text)


def redact_output(
    text: str,
    *,
    command: str | None = None,
    force: bool = False,
    scope: Scope | None = None,
) -> str:
    """``text`` with secrets masked; see the module docstring for the rules."""
    if not text:
        return text
    current = scope if scope is not None else scope_or_none()
    indus_home = current.home if current is not None else None
    assignments = (
        command is None or is_env_dump(command) or reads_secret_file(command, indus_home=indus_home)
    )
    try:
        if current is None:
            out = redact_text(_mask_extra_shapes(text), assignments=assignments)
            return out if assignments else _diagnostics(out, redact_text)
        redactor = _redactor_for(current)
        if not (force or redactor.enabled):
            return text
        out = redactor.redact(_mask_extra_shapes(text), force=True, assignments=assignments)
        if assignments:
            return out
        return _diagnostics(out, lambda line: redactor.redact(line, force=True))
    except Exception as exc:  # never hand back unmasked text because masking broke
        log.warning("output redaction failed: %s", type(exc).__name__)
        return WITHHELD


def spill_redactor(scope: Scope) -> Callable[[str], str]:
    """The force-mode redactor spill files, receipts and overflow logs use (P04-12 interface)."""

    def redact(text: str) -> str:
        return redact_output(text, force=True, scope=scope)

    return redact
