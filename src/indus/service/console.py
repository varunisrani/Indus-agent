# pyright: strict
"""The admin console engine: a curated set of ``indus`` commands, never a shell (P24-36).

It sits here, below both surfaces that use it — the dashboard's ``/api/console`` socket
(``indus.web.console``) and the local ``indus console`` REPL (``indus.cli.console_engine``) —
because the two may not import each other.

``ConsoleEngine.execute(line)`` turns one line into a ``ConsoleResult``:

- the line is split into words like a shell would (quotes group words) but nothing a shell does
  happens: a line holding a pipe, a redirect, ``;``, ``&``, ``$(`` or a backtick is refused, and so
  is a first word that is a flag;
- a leading ``indus`` word is dropped; the built-ins ``help [command]``, ``history``, ``clear``,
  ``version``, ``exit`` and ``quit`` are answered here;
- every other line must start with one of the paths in ``COMMANDS`` (longest match wins); an
  unknown one is refused with up to three close matches; a command's ``refused_args``
  (``logs --follow``, ``doctor --fix``) are refused too;
- a command with a confirmation question returns ``confirm_required`` first and runs only when
  ``execute`` is called again with ``confirmed=True`` for the same line;
- a command runs as its own process, ``python -m indus <words>``, for the console's profile (the
  env from ``safety.child_env`` with ``INDUS_HOME`` pinned to the profile; never this process's
  env), stdin closed (no prompt can wait), in a new process group — on native Windows in its own
  job object instead (``local_windows.start_windows_process``; a start the job calls refuse is a
  "could not start" answer);
- output beyond ``output_limit`` characters (50,000) is cut with a marker that says how many were
  left out; credential shapes are masked.

Cancellation: each run carries a ``CancelToken``; cancel (or the caller's timeout) kills the
command's process group and every descendant — an agent run the command started included (on
Windows: the job is terminated).
"""

from __future__ import annotations

import contextlib
import difflib
import shlex
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol

from indus.backends.contract import BackendError
from indus.backends.local import LocalProcess, force_kill_tree
from indus.backends.local_windows import start_windows_process
from indus.foundation.redact import redact_text
from indus.foundation.scope import Scope
from indus.platform.facts import os_family
from indus.safety.child_env import child_env

__all__ = [
    "COMMANDS",
    "OUTPUT_LIMIT",
    "PROMPT",
    "CancelToken",
    "CommandRunner",
    "ConsoleCommand",
    "ConsoleCommandError",
    "ConsoleEngine",
    "ConsoleResult",
    "RunOutput",
    "SubprocessRunner",
]

OUTPUT_LIMIT: Final = 50_000
PROMPT: Final = "indus> "
_MAX_CAPTURE: Final = 4 * OUTPUT_LIMIT

type Status = Literal["ok", "error", "confirm_required", "clear", "exit"]

_SHELL_WORDS: Final = frozenset({"|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "2>", "2>>"})
_SHELL_CHARS: Final = frozenset("|<>;&`")
_BUILTINS: Final = ("help", "history", "clear", "version", "exit", "quit")


@dataclass(frozen=True)
class ConsoleResult:
    """What one line did. ``output``/``errors`` are the command's stdout/stderr text."""

    status: Status
    output: str = ""
    errors: str = ""
    command: str = ""
    message: str = ""
    """The confirmation question (``confirm_required``) or the refusal (``error``)."""


class ConsoleCommandError(Exception):
    """A refusal or failure the person sees as is."""


type Handler = Callable[["ConsoleEngine", list[str], "CancelToken"], str]


@dataclass(frozen=True)
class ConsoleCommand:
    """One allowed command path. ``confirm`` non-empty → it changes something and asks first."""

    path: tuple[str, ...]
    usage: str
    summary: str
    confirm: str = ""
    refused_args: frozenset[str] = frozenset()
    """Arguments this console never passes on (``--follow``: it would never end)."""
    handler: Handler | None = None
    """Run instead of ``indus <path> <args>`` (tests)."""

    @property
    def mutating(self) -> bool:
        return bool(self.confirm)


def _cmd(
    path: str, usage: str, summary: str, confirm: str = "", refused: str = ""
) -> ConsoleCommand:
    return ConsoleCommand(tuple(path.split()), usage, summary, confirm, frozenset(refused.split()))


COMMANDS: Final[tuple[ConsoleCommand, ...]] = (
    _cmd("status", "status", "Model, provider and store at a glance."),
    _cmd("doctor", "doctor", "Check the setup (never repairs from here).", refused="--fix"),
    _cmd("logs", "logs [name] [-n N]", "Recent log lines.", refused="-f --follow"),
    _cmd("sessions list", "sessions list [--limit N]", "Recent sessions."),
    _cmd("sessions stats", "sessions stats", "Session store statistics."),
    _cmd("config show", "config show", "The effective settings (credentials masked)."),
    _cmd("config get", "config get <key>", "One setting."),
    _cmd("config path", "config path", "Where config.yaml lives."),
    _cmd("config check", "config check", "Check config.yaml against the schema."),
    _cmd("cron list", "cron list [--all]", "Scheduled jobs."),
    _cmd("cron status", "cron status", "Whether the scheduler runs, and the next jobs."),
    _cmd("cron runs", "cron runs <job>", "Recent runs of a job."),
    _cmd("profile list", "profile list", "Profiles on this machine."),
    _cmd("profile show", "profile show [name]", "One profile's details."),
    _cmd("skills list", "skills list", "Installed skills."),
    _cmd("plugins list", "plugins list", "Plugins and their state."),
    _cmd("mcp list", "mcp list", "Configured MCP servers."),
    _cmd("hooks list", "hooks list", "Shell hooks and their approval."),
    _cmd("tools list", "tools list", "Toolsets per platform."),
    _cmd("memory status", "memory status", "Memory provider status."),
    _cmd("checkpoints status", "checkpoints status", "File checkpoint store size."),
    _cmd("checkpoints list", "checkpoints list", "File checkpoints."),
    _cmd("pairing list", "pairing list", "Pairing requests and approved users."),
    _cmd("webhook list", "webhook list", "Webhook subscriptions."),
    _cmd("bundles list", "bundles list", "Skill bundles."),
    _cmd("gateway status", "gateway status", "Whether the messaging gateway runs."),
    _cmd("config set", "config set <key> <value>", "Change one setting.", "Change this setting?"),
    _cmd("cron pause", "cron pause <job>", "Pause a job.", "Pause this job?"),
    _cmd("cron resume", "cron resume <job>", "Resume a paused job.", "Resume this job?"),
    _cmd("cron run", "cron run <job>", "Run a job now and wait for it.", "Run this job now?"),
    _cmd(
        "sessions rename",
        "sessions rename <session> <title>",
        "Rename a session.",
        "Rename this session?",
    ),
    _cmd("plugins enable", "plugins enable <name>", "Enable a plugin.", "Enable this plugin?"),
    _cmd("plugins disable", "plugins disable <name>", "Disable a plugin.", "Disable this plugin?"),
    _cmd("mcp enable", "mcp enable <name>", "Enable an MCP server.", "Enable this server?"),
    _cmd("mcp disable", "mcp disable <name>", "Disable an MCP server.", "Disable this server?"),
    _cmd(
        "pairing approve",
        "pairing approve <platform> <code>",
        "Approve a pairing request.",
        "Approve this pairing request?",
    ),
    _cmd(
        "pairing revoke",
        "pairing revoke <platform> <user>",
        "Revoke a paired user.",
        "Revoke this user?",
    ),
)
"""The allowed commands, in help order; each path must exist in ``indus --help`` (tested)."""


# -- cancellation --------------------------------------------------------------------------------


class CancelToken:
    """Set once by cancel or timeout; interrupters run when it is set (each in isolation)."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._interrupters: list[Callable[[], None]] = []

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def on_cancel(self, interrupt: Callable[[], None]) -> Callable[[], None]:
        """Call ``interrupt`` when cancelled (at once when already cancelled); returns a remover."""
        with self._lock:
            already = self._event.is_set()
            if not already:
                self._interrupters.append(interrupt)
        if already:
            _quietly(interrupt)

        def remove() -> None:
            with self._lock, contextlib.suppress(ValueError):
                self._interrupters.remove(interrupt)

        return remove

    def cancel(self) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            pending, self._interrupters = self._interrupters, []
        for interrupt in pending:
            _quietly(interrupt)


def _quietly(fn: Callable[[], None]) -> None:
    with contextlib.suppress(Exception):
        fn()


# -- running a command ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunOutput:
    code: int | None
    stdout: str
    stderr: str
    cancelled: bool = False


class CommandRunner(Protocol):
    def __call__(self, scope: Scope, words: Sequence[str], token: CancelToken) -> RunOutput: ...


@dataclass(frozen=True)
class SubprocessRunner:
    """``python -m indus <words>`` for ``scope``, killed (with its whole tree) on cancel."""

    python: str = field(default_factory=lambda: sys.executable)

    def __call__(self, scope: Scope, words: Sequence[str], token: CancelToken) -> RunOutput:
        env = child_env(scope)
        env["NO_COLOR"] = "1"
        argv = [self.python, "-m", "indus", *words]
        if os_family() == "win32":  # no process groups to signal: a job object holds the tree
            try:
                job = start_windows_process(
                    argv, cwd=str(scope.home), env=env, stdin=None, separate_stderr=True
                )
            except BackendError as exc:
                raise OSError(str(exc)) from exc
            popen, stop, release = job.popen, job.terminate_tree, job.release
        else:
            popen = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(scope.home),
                env=env,
                start_new_session=True,
                close_fds=True,
            )
            proc = LocalProcess(popen)

            def stop() -> None:
                force_kill_tree(proc)

            def release() -> None:
                return None

        remove = token.on_cancel(stop)
        try:
            out, err = popen.communicate()
        finally:
            remove()
            release()
        return RunOutput(
            code=popen.returncode,
            stdout=out[:_MAX_CAPTURE].decode("utf-8", "replace"),
            stderr=err[:_MAX_CAPTURE].decode("utf-8", "replace"),
            cancelled=token.cancelled,
        )


# -- the engine ----------------------------------------------------------------------------------


def _split(line: str) -> list[str]:
    try:
        return shlex.split(line, posix=True)
    except ValueError as exc:
        raise ConsoleCommandError(f"Could not read the line: {exc}.") from exc


def _shell_like(line: str, words: Sequence[str]) -> bool:
    return "$(" in line or any(w in _SHELL_WORDS for w in words) or bool(_SHELL_CHARS & set(line))


def _default_commands() -> dict[tuple[str, ...], ConsoleCommand]:
    return {c.path: c for c in COMMANDS}


def _no_secrets() -> Sequence[str]:
    return ()


@dataclass
class ConsoleEngine:
    """One console's engine (see the module notes)."""

    scope: Scope
    runner: CommandRunner = field(default_factory=SubprocessRunner)
    output_limit: int = OUTPUT_LIMIT
    commands: dict[tuple[str, ...], ConsoleCommand] = field(default_factory=_default_commands)
    history: list[str] = field(default_factory=list[str])
    secrets: Callable[[], Sequence[str]] = _no_secrets

    def register(self, command: ConsoleCommand) -> None:
        self.commands[command.path] = command

    def execute(
        self, line: str, *, confirmed: bool = False, token: CancelToken | None = None
    ) -> ConsoleResult:
        raw = line.strip()
        if not raw:
            return ConsoleResult("ok")
        try:
            words = _split(raw)
            if words and words[0] == "indus":
                words = words[1:]
            if not words:
                return ConsoleResult("ok", output=self.help_text(), command=raw)
            if _shell_like(raw, words):
                raise ConsoleCommandError(
                    "The console runs one indus command at a time; shell syntax "
                    "(pipes, redirects, ;, &, $( ), backticks) is not available."
                )
            builtin = self._builtin(words, raw)
            if builtin is not None:
                return builtin
            command, args = self._resolve(words)
            refused = sorted({a.split("=", 1)[0] for a in args} & command.refused_args)
            if refused:
                raise ConsoleCommandError(
                    f"`{' '.join(command.path)} {refused[0]}` is not available in the console."
                )
            if command.mutating and not confirmed:
                return ConsoleResult("confirm_required", command=raw, message=command.confirm)
            output, errors, failed = self._run(command, args, token or CancelToken())
            self.history.append(raw)
            output, errors = self._cap(self._mask(output), self._mask(errors))
            if failed:
                return ConsoleResult("error", output, errors, raw, failed)
            return ConsoleResult("ok", output, errors, raw)
        except ConsoleCommandError as exc:
            return ConsoleResult("error", command=raw, message=self._mask(str(exc)))

    def _builtin(self, words: list[str], raw: str) -> ConsoleResult | None:
        head = words[0]
        if head not in _BUILTINS:
            return None
        if head == "help":
            subject = words[1:]
            if subject:
                command, _ = self._resolve(subject)
                text = f"{command.usage}\n{command.summary}"
                if command.mutating:
                    text += "\n(asks before it runs)"
                return ConsoleResult("ok", output=text, command=raw)
            return ConsoleResult("ok", output=self.help_text(), command=raw)
        if head == "history":
            lines = [f"{n}: {entry}" for n, entry in enumerate(self.history, start=1)]
            return ConsoleResult("ok", output="\n".join(lines) or "No history yet.", command=raw)
        self.history.append(raw)
        if head == "clear":
            return ConsoleResult("clear", command=raw)
        if head == "version":
            import indus

            return ConsoleResult("ok", output=f"Indus Agent {indus.__version__}", command=raw)
        return ConsoleResult("exit", command=raw)

    def help_text(self) -> str:
        width = max(len(c.usage) for c in self.commands.values())
        lines = ["Indus console: one indus command per line.", ""]
        for command in self.commands.values():
            mark = "*" if command.mutating else " "
            lines.append(f" {mark} {command.usage.ljust(width)}  {command.summary}")
        lines += [
            "",
            "* asks before it runs",
            "Also: help <command>, history, clear, version, exit",
        ]
        return "\n".join(lines)

    def _resolve(self, words: Sequence[str]) -> tuple[ConsoleCommand, list[str]]:
        first = words[0]
        if first.startswith("-"):
            raise ConsoleCommandError(f"{first} is not available in the console.")
        for size in range(min(3, len(words)), 0, -1):
            found = self.commands.get(tuple(words[:size]))
            if found is not None:
                return found, list(words[size:])
        probe = " ".join(words[:2])
        known = [" ".join(path) for path in self.commands]
        close = difflib.get_close_matches(probe, known, n=3, cutoff=0.5)
        if not close and len(words) > 1:
            close = difflib.get_close_matches(first, known, n=3, cutoff=0.5)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise ConsoleCommandError(f"`{probe}` is not a console command.{hint}")

    def _run(
        self, command: ConsoleCommand, args: list[str], token: CancelToken
    ) -> tuple[str, str, str]:
        """``(stdout, stderr, failure message or "")``."""
        name = " ".join(command.path)
        if command.handler is not None:
            try:
                return command.handler(self, args, token).rstrip(), "", ""
            except ConsoleCommandError as exc:
                return "", "", str(exc)
        try:
            run = self.runner(self.scope, [*command.path, *args], token)
        except OSError as exc:
            return "", "", f"`{name}` could not start: {exc.strerror or exc}"
        if run.cancelled:
            return run.stdout.rstrip(), run.stderr.rstrip(), "Cancelled: the command was stopped."
        failure = "" if run.code == 0 else f"`{name}` ended with exit code {run.code}."
        return run.stdout.rstrip(), run.stderr.rstrip(), failure

    def _mask(self, text: str) -> str:
        return redact_text(text, self.secrets()) if text else text

    def _cap(self, output: str, errors: str) -> tuple[str, str]:
        total = len(output) + len(errors)
        if total <= self.output_limit:
            return output, errors
        omitted = total - self.output_limit
        marker = f"\n... output cut: {omitted} more characters were left out"
        if len(output) >= self.output_limit:
            return output[: self.output_limit] + marker, ""
        room = self.output_limit - len(output)
        return output, errors[:room] + marker
