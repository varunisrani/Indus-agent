# pyright: strict
"""Sudo support for shell backends (P05-10).

A command that calls ``sudo`` on a machine that wants a password would hang on a prompt nobody
sees, or fail with "a password is required". When Indus knows the password it rewrites each
literal ``sudo`` word to ``sudo -S -p ''`` and puts one password line per rewritten call in front
of the command's stdin. The password never goes on a command line (R02.7), into a log, or back to
the model (it is masked in the output).

What is rewritten (``rewrite_sudo``): ``sudo`` (or a path ending in ``/sudo``) at a command
position (start, after ``; & && || ( ) {``, a newline, ``!``, ``time`` and the ``if``/``while``
keywords, after leading ``NAME=value`` words) and after ``env`` with ordinary options and
assignments. Never: quoted words (``bash -c 'sudo …'``), ``env -S``, dynamic paths (``$SUDO``,
``$(which sudo)``, backticks, globs, ``~``), command substitutions, comments, here-document
bodies, and a ``sudo`` whose stdin is not the command's own (after ``|``, or with a ``<``,
``<<`` or ``<<<`` redirection): it would read the password from the wrong place.

When (``SudoSupport.before``, called by ``SpawnedEnvironment.execute``): there is something to
rewrite, the backend takes stdin through a pipe (a heredoc backend would put the password into the
script text), and ``sudo -n true`` (3 s) says a password is needed. A passwordless account, or a
probe that fails or times out, gets no rewrite (fail closed). The password comes from the
session's cache, then the profile's secret store (``sudo_password``), then a masked ``sudo``
server request (45 s) when the terminal tool installed a caller (``use_sudo_caller``) for an
interactive surface. Without one the run is headless.

Afterwards (``SudoSupport.after``): the password is masked in the output; a headless "password
required" failure gets a tip to store the password locally (never in chat); a rejected password
sets ``sudo_auth_failed`` and drops a cached or prompted copy (``sudo_cache_cleared``); an account
that may not use sudo gets a plain note and no promise of a new prompt.

Where (P11 integration, security): the password of this machine never leaves it. Every hook is
keyed by the backend environment it serves (``target``; ``HOST_LOCAL`` for the local shell) and
the cache key carries it, so a password typed for one place is never fed to another. A host-local
hook refuses to rewrite on an environment that is not host-local (defence in depth). Remote and
container backends get a hook only through ``backend_sudo``, and only when the profile stores a
password for that backend (``<backend>_sudo_password``); such a hook reads only that secret and
never prompts, so the person is never asked for "your sudo password" by a remote host.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Awaitable, Callable, Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol

from indus.backends.contract import BackendFlags, ExecResult
from indus.foundation.scope import Scope
from indus.protocol.requests_port import RequestNotAnswered, ServerRequestPort
from indus.protocol.server_requests import SudoAnswer
from indus.safety.redaction import redact_output

__all__ = [
    "MASK",
    "PASSWORDS",
    "PROBE_TIMEOUT_S",
    "PROMPT_TIMEOUT_S",
    "HOST_LOCAL",
    "SECRET_NAMES",
    "SudoCaller",
    "SudoPasswordCache",
    "SudoRun",
    "SudoSupport",
    "SudoTarget",
    "backend_secret_names",
    "backend_sudo",
    "broker_asker",
    "rewrite_sudo",
    "use_sudo_caller",
]

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 3.0
PROMPT_TIMEOUT_S = 45.0
SECRET_NAMES = ("sudo_password", "SUDO_PASSWORD")
HOST_LOCAL = "local"
MASK = "[redacted]"
REWRITE_FLAGS = " -S -p ''"

# -- the rewrite -----------------------------------------------------------------------------------

_OPERATOR_CHARS = frozenset(";&|()")
_WORD_END = frozenset(" \t\n;&|()<>")
_DYNAMIC = frozenset("$`*?[]~{}")
_KEYWORDS = frozenset(
    {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "time", "case"}
)
_ENV_VALUE_OPTIONS = frozenset({"-u", "--unset", "-C", "--chdir", "-a", "--argv0"})
_ENV_FLAG_OPTIONS = frozenset({"-i", "--ignore-environment", "-0", "--null", "-v", "--debug", "-"})


def _is_assignment(word: str) -> bool:
    name, eq, _ = word.partition("=")
    return bool(eq) and bool(name) and not name[0].isdigit() and name.replace("_", "a").isalnum()


@dataclass
class _Word:
    text: str
    quoted: bool
    dynamic: bool


class _Rewriter:
    """One pass over the command, copying it and appending the flags after each real ``sudo``."""

    def __init__(self, text: str) -> None:
        self.s = text
        self.i = 0
        self.out: list[str] = []
        self.count = 0
        self.at_command = True
        self.env_state: Literal["off", "options", "value"] = "off"
        self.heredocs: list[tuple[str, bool]] = []
        self.piped = False  # this command's stdin is a pipe, not the command line's stdin
        self.inserted: int | None = None  # where this command's sudo flags went in ``out``

    # -- scanning helpers ----------------------------------------------------------------------

    def _skip_quote(self, start: int) -> int:
        """Index just past the quoted run that starts at ``start`` (unterminated: end of text)."""
        s, quote = self.s, self.s[start]
        j = start + 1
        while j < len(s):
            if quote == '"' and s[j] == "\\":
                j += 2
                continue
            if s[j] == quote:
                return j + 1
            if quote == '"' and s.startswith("$(", j):
                j = self._skip_group(j + 1, "(", ")")
                continue
            j += 1
        return len(s)

    def _skip_group(self, start: int, opener: str, closer: str) -> int:
        """Index just past the group opened at ``start`` (``s[start] == opener``)."""
        s, depth, j = self.s, 0, start
        while j < len(s):
            ch = s[j]
            if ch == "\\":
                j += 2
                continue
            if ch in "'\"`" and j != start:
                j = self._skip_quote(j) if ch != "`" else self._skip_backtick(j)
                continue
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        return len(s)

    def _skip_backtick(self, start: int) -> int:
        s, j = self.s, start + 1
        while j < len(s):
            if s[j] == "\\":
                j += 2
                continue
            if s[j] == "`":
                return j + 1
            j += 1
        return len(s)

    def _read_word(self) -> _Word:
        s, start = self.s, self.i
        quoted = dynamic = False
        j = start
        if s.startswith(("<(", ">("), j):
            j = self._skip_group(j + 1, "(", ")")
            dynamic = True
        while j < len(s) and s[j] not in _WORD_END:
            ch = s[j]
            if ch == "\\":
                quoted = True
                j += 2
            elif ch in "'\"":
                quoted = True
                j = self._skip_quote(j)
            elif ch == "`":
                dynamic = True
                j = self._skip_backtick(j)
            elif ch == "$" and j + 1 < len(s) and s[j + 1] in "({":
                dynamic = True
                j = self._skip_group(j + 1, s[j + 1], ")" if s[j + 1] == "(" else "}")
            else:
                if ch in _DYNAMIC:
                    dynamic = True
                j += 1
        self.i = min(j, len(s))
        return _Word(s[start : self.i], quoted, dynamic)

    def _copy_to(self, end: int) -> None:
        self.out.append(self.s[self.i : end])
        self.i = end

    # -- grammar ---------------------------------------------------------------------------------

    def _heredoc_bodies(self) -> None:
        """At the start of a line after ``<<WORD``: copy the bodies through their end lines."""
        s = self.s
        for delimiter, strip_tabs in self.heredocs:
            while self.i < len(s):
                end = s.find("\n", self.i)
                end = len(s) if end == -1 else end + 1
                line = s[self.i : end].rstrip("\n")
                self._copy_to(end)
                if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                    break
        self.heredocs = []

    def _redirect(self, fd: str = "") -> None:
        s = self.s
        for op in ("<<<", "<<-", "&>>", "<<", ">>", "<&", ">&", "<>", ">|", "&>", "<", ">"):
            if s.startswith(op, self.i):
                break
        else:  # pragma: no cover - only called on < > &
            op = s[self.i]
        self._copy_to(self.i + len(op))
        if op.startswith("<") and fd in ("", "0"):
            self._undo_insert()  # sudo would read the password from this redirection instead
        while self.i < len(s) and s[self.i] in " \t":
            self._copy_to(self.i + 1)
        if self.i >= len(s) or s[self.i] in "\n;|&()":
            return
        target = self._read_word()
        self.out.append(target.text)
        if op in ("<<", "<<-"):
            delimiter = target.text.replace("'", "").replace('"', "").replace("\\", "")
            self.heredocs.append((delimiter, op == "<<-"))

    def _command_word(self, word: _Word) -> None:
        text = word.text
        if self.env_state == "value":
            self.env_state = "options"
            return
        if self.env_state == "options":
            if text == "--":
                return
            if text in _ENV_FLAG_OPTIONS or (
                text.startswith(("--unset=", "--chdir=", "--argv0="))
                or (text[:2] in ("-u", "-C", "-a") and len(text) > 2)
            ):
                return
            if text in _ENV_VALUE_OPTIONS:
                self.env_state = "value"
                return
            if text.startswith("-"):  # -S / --split-string and anything unknown: stop
                self.env_state = "off"
                self.at_command = False
                return
            if _is_assignment(text) and not word.quoted:
                return
            self.env_state = "off"  # this word is the command env runs
        elif _is_assignment(text):
            return  # NAME=value before the command
        elif text in _KEYWORDS and not word.quoted:
            return
        self.at_command = False
        if word.quoted or word.dynamic:
            return
        program = text.rsplit("/", 1)[-1]
        if program == "sudo" and not self.piped:
            self.out.append(REWRITE_FLAGS)
            self.inserted = len(self.out) - 1
            self.count += 1
        elif program == "env":
            self.env_state = "options"
            self.at_command = True

    def run(self) -> tuple[str, int]:
        s = self.s
        while self.i < len(s):
            ch = s[self.i]
            if ch in " \t":
                self._copy_to(self.i + 1)
            elif ch == "\n":
                self._copy_to(self.i + 1)
                self._end_of_command()
                if self.heredocs:
                    self._heredoc_bodies()
            elif ch == "\\" and s.startswith("\\\n", self.i):
                self._copy_to(self.i + 2)  # line continuation: same command goes on
            elif ch == "#" and (self.i == 0 or s[self.i - 1] in " \t\n;&|()"):
                end = s.find("\n", self.i)
                self._copy_to(len(s) if end == -1 else end)
            elif (
                ch in "<>"
                and not s.startswith(("<(", ">("), self.i)
                or ch == "&"
                and s.startswith("&>", self.i)
            ):
                self._redirect()
            elif ch in _OPERATOR_CHARS:
                two = s[self.i : self.i + 2]
                width = 2 if two in ("&&", "||", "|&", ";;", ";&") else 1
                self._copy_to(self.i + width)
                if ch == ")":
                    self.at_command = False
                    self.env_state = "off"
                else:
                    self._end_of_command()
                    self.piped = two in ("|&",) or (ch == "|" and two != "||")
            else:
                word = self._read_word()
                if word.text.isdigit() and self.i < len(s) and s[self.i] in "<>":
                    self.out.append(word.text)  # an fd number before a redirection
                    self._redirect(word.text)
                    continue
                self.out.append(word.text)
                if self.at_command:
                    self._command_word(word)
        return "".join(self.out), self.count

    def _end_of_command(self) -> None:
        self.at_command = True
        self.env_state = "off"
        self.piped = False
        self.inserted = None

    def _undo_insert(self) -> None:
        if self.inserted is not None:
            self.out[self.inserted] = ""
            self.inserted = None
            self.count -= 1


def rewrite_sudo(command: str) -> tuple[str, int]:
    """``command`` with every real ``sudo`` word followed by `` -S -p ''``, and how many."""
    return _Rewriter(command).run()


# -- password sources ------------------------------------------------------------------------------

type CacheKey = tuple[str, str, str]


class SudoPasswordCache:
    """Passwords a person typed, per profile home, session and backend environment (never shared
    across any of them)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[CacheKey, str] = {}

    def get(self, key: CacheKey) -> str | None:
        with self._lock:
            return self._values.get(key)

    def put(self, key: CacheKey, password: str) -> None:
        with self._lock:
            self._values[key] = password

    def clear(self, key: CacheKey) -> bool:
        with self._lock:
            return self._values.pop(key, None) is not None

    def clear_all(self) -> None:
        with self._lock:
            self._values.clear()


PASSWORDS = SudoPasswordCache()

type PasswordAsker = Callable[[str], Awaitable[str | None]]


@dataclass(frozen=True)
class SudoCaller:
    """Who runs the current command: ``ask`` shows a masked prompt, None means headless."""

    ask: PasswordAsker | None = None


_CALLER: ContextVar[SudoCaller | None] = ContextVar("indus_sudo_caller", default=None)


def broker_asker(broker: ServerRequestPort, turn_id: str | None = None) -> PasswordAsker:
    """A masked prompt through the session's broker (``sudo`` server request, 45 s)."""

    async def ask(reason: str) -> str | None:
        try:
            answer = await broker.ask(
                "sudo", {"reason": reason}, timeout=PROMPT_TIMEOUT_S, turn_id=turn_id
            )
        except RequestNotAnswered:
            return None
        if isinstance(answer, SudoAnswer) and answer.password:
            return answer.password
        return None

    return ask


@contextmanager
def use_sudo_caller(
    *, broker: ServerRequestPort | None, turn_id: str | None = None, interactive: bool = True
) -> Generator[SudoCaller]:
    """Around ``env.execute``: lets the sudo hook ask this call's person for a password.
    Delegated children and unattended runs pass ``interactive=False`` (headless)."""
    caller = SudoCaller(broker_asker(broker, turn_id) if broker and interactive else None)
    token = _CALLER.set(caller)
    try:
        yield caller
    finally:
        _CALLER.reset(token)


# -- the hook --------------------------------------------------------------------------------------


class SudoTarget(Protocol):
    """What the hook needs from an environment (``SpawnedEnvironment`` fits)."""

    @property
    def flags(self) -> BackendFlags: ...

    async def probe_sudo(self, timeout: float) -> ExecResult: ...


type Source = Literal["cache", "secret", "prompt"]
type ProbeVerdict = Literal["passwordless", "password", "no_sudo", "unknown"]


@dataclass(frozen=True)
class SudoRun:
    """What ``before`` did for one command; ``after`` reads it."""

    rewritten: bool
    count: int
    source: Source | None = None
    can_prompt: bool = False
    no_sudo: bool = False
    password: str = field(default="", repr=False)


_NEEDS_PASSWORD = ("a password is required", "a terminal is required", "no tty present")
_WRONG_PASSWORD = ("incorrect password attempt", "sorry, try again", "authentication failed")
_NO_SUDO = ("is not in the sudoers file", "may not run sudo", "is not allowed to run sudo")


def _classify_probe(result: ExecResult) -> ProbeVerdict:
    if result.timed_out or result.interrupted:
        return "unknown"
    text = result.output.lower()
    if result.returncode == 0:
        return "passwordless"
    if any(marker in text for marker in _NO_SUDO):
        return "no_sudo"
    if any(marker in text for marker in _NEEDS_PASSWORD):
        return "password"
    return "unknown"


class SudoSupport:
    """The sudo hook of one environment (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        *,
        session_id: str | None = None,
        cache: SudoPasswordCache | None = None,
        probe_timeout: float = PROBE_TIMEOUT_S,
        target: str = HOST_LOCAL,
        secret_names: Sequence[str] = SECRET_NAMES,
        can_prompt: bool | None = None,
    ) -> None:
        self.scope = scope
        self.target = target
        self.key: CacheKey = (str(scope.home), session_id or "default", target)
        self.cache = cache if cache is not None else PASSWORDS
        self.probe_timeout = probe_timeout
        self.secret_names = tuple(secret_names)
        # Only the host's own hook may ask the person; a remote one uses its stored secret only.
        self.can_prompt = (target == HOST_LOCAL) if can_prompt is None else can_prompt

    def _configured(self) -> str | None:
        if self.scope.secrets is None:
            return None
        for name in self.secret_names:
            try:
                value = self.scope.secrets.get(name)
            except Exception:
                log.debug("secret store lookup failed", exc_info=True)
                continue
            if value:
                return value
        return None

    async def _probe(self, env: SudoTarget) -> ProbeVerdict:
        try:
            result = await env.probe_sudo(self.probe_timeout)
        except Exception as exc:
            log.info("sudo probe failed (%s); leaving the command as written", type(exc).__name__)
            return "unknown"
        return _classify_probe(result)

    def _reason(self, command: str) -> str:
        shown = redact_output(command, force=True, scope=self.scope)
        if len(shown) > 400:
            shown = shown[:400] + "…"
        return f"This command needs your administrator (sudo) password:\n{shown}"

    async def before(
        self, env: SudoTarget, command: str, stdin: str | None
    ) -> tuple[str, str | None, SudoRun | None]:
        """The command and stdin to run, and what to check afterwards (None: nothing)."""
        rewritten, count = rewrite_sudo(command)
        if count == 0:
            return command, stdin, None
        if self.target == HOST_LOCAL and not env.flags.host_local:
            # This machine's password never goes to a remote host or into a container.
            return command, stdin, None
        caller = _CALLER.get()
        asker = caller.ask if caller is not None and self.can_prompt else None
        if env.flags.stdin_mode == "heredoc":
            # The password would become part of the script text; let sudo fail on its own.
            return command, stdin, SudoRun(False, count, can_prompt=False)
        verdict = await self._probe(env)
        if verdict == "passwordless":
            return command, stdin, None
        if verdict != "password":
            return command, stdin, SudoRun(False, count, no_sudo=verdict == "no_sudo")
        source: Source | None = None
        password = self.cache.get(self.key)
        if password:
            source = "cache"
        else:
            password = self._configured()
            if password:
                source = "secret"
        if not password and asker is not None:
            password = await asker(self._reason(command))
            if password:
                source = "prompt"
                self.cache.put(self.key, password)
        if not password or source is None:
            return command, stdin, SudoRun(False, count, can_prompt=asker is not None)
        log.info("sudo password supplied on stdin for %d call(s) (from %s)", count, source)
        lines = (password + "\n") * count
        run = SudoRun(True, count, source, can_prompt=asker is not None, password=password)
        return rewritten, lines + (stdin or ""), run

    def after(self, run: SudoRun, result: ExecResult) -> ExecResult:
        """Mask the password and explain sudo failures (see the module notes)."""
        output = result.output
        if run.password:
            output = output.replace(run.password, MASK)
        lowered = output.lower()
        auth_failed = cache_cleared = False
        notes: list[str] = []
        no_sudo = run.no_sudo or any(marker in lowered for marker in _NO_SUDO)
        if no_sudo:
            notes.append(
                "This account is not allowed to use sudo on this machine; a password will not "
                "help. Run the command without sudo or ask the machine's administrator."
            )
        elif run.rewritten and any(marker in lowered for marker in _WRONG_PASSWORD):
            auth_failed = True
            if run.source in ("cache", "prompt"):
                cache_cleared = self.cache.clear(self.key) or run.source == "prompt"
                again = (
                    " The user will be asked for it again the next time sudo is used."
                    if run.can_prompt
                    else ""
                )
                notes.append(f"sudo rejected the password; the remembered copy was cleared.{again}")
            else:
                notes.append(
                    f"sudo rejected the stored {self.secret_names[0]}. The user needs to update "
                    "it in this profile's secret store on the machine running Indus."
                )
        elif not run.rewritten and any(marker in lowered for marker in _NEEDS_PASSWORD):
            if run.can_prompt:
                notes.append("sudo needed a password and none was given, so the command failed.")
            else:
                notes.append(
                    "Tip: this session cannot ask for a sudo password. The user can store it in "
                    f"this profile's secret store as {self.secret_names[0]} on the machine "
                    "running Indus (never paste it into chat), or run the command without sudo."
                )
        if output != result.output or notes or auth_failed:
            if notes:
                output = output.rstrip("\n") + "\n\n" + "\n".join(notes)
            return replace(
                result,
                output=output,
                sudo_auth_failed=auth_failed,
                sudo_cache_cleared=cache_cleared,
            )
        return result


def backend_secret_names(backend: str) -> tuple[str, str]:
    """The secret names that hold the sudo password of a remote or container backend."""
    return (f"{backend}_sudo_password", f"{backend.upper()}_SUDO_PASSWORD")


def backend_sudo(
    scope: Scope, *, backend: str, target: str, session_id: str | None = None
) -> SudoSupport | None:
    """The sudo hook of a backend that is not this machine (ssh, docker, ...), or None.

    Only when the profile stores a password for that backend (``backend_secret_names``); the
    hook reads that secret alone, is keyed by ``target`` (``ssh:user@host:port``,
    ``docker:<key>``), and never prompts. Without such a secret the command runs as written and
    sudo fails on its own: the local password is never sent anywhere (module notes)."""
    if target == HOST_LOCAL or scope.secrets is None:
        return None
    names = backend_secret_names(backend)
    for name in names:
        try:
            value = scope.secrets.get(name)
        except Exception:
            log.debug("secret store lookup failed", exc_info=True)
            continue
        if value:
            return SudoSupport(
                scope,
                session_id=session_id,
                target=target,
                secret_names=names,
                can_prompt=False,
            )
    return None
