"""The built-in slash commands the agent side runs (``slash.exec`` / ``command.dispatch``), written
once for the TUI's in-process bridge (P12-31) and the daemon's WebSocket (W48-h).

A transport implements ``SlashHost`` (how it titles, takes back, branches, compresses, stops and
steers a session) and hands ``run`` / ``slash_exec`` / ``dispatch`` its handler table: the shared
``HANDLERS`` plus its own (the bridge adds ``queue``, ``yolo``, ``reasoning``, ``fast``). A name in
the host's ``refused`` table is answered with its reason, never run: ``SLASH_NOT_OWNED`` from
``slash.exec`` (so a client asks ``command.dispatch``) and ``UNSUPPORTED_COMMAND`` from
``command.dispatch`` (the client then shows the reason). The same holds for a quick ``exec``
command when the host has ``quick_exec_refusal``. Who runs which built-in is
``picker_reads.SERVER_COMMANDS`` / ``CLIENT_COMMANDS``; any other built-in is "not owned".

A registry entry whose busy policy is ``reject`` is refused with ``SESSION_BUSY`` while a turn
runs; a handler that fails or passes the host's ``timeout`` (hosts keep it at least
``MIN_TIMEOUT_S``) answers
``SLASH_WORKER_FAILED``. ``command.dispatch`` resolves, in order, a quick command (``exec`` runs
through the host's ``run_quick``; ``alias`` names another command), a skill or skill bundle (its
expansion is the prompt to send), then a built-in (``exec`` with ``slash.exec``'s output).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from importlib import metadata
from typing import TYPE_CHECKING, Any, Protocol

from indus.foundation.blocking import run_blocking
from indus.foundation.scope import Scope
from indus.protocol.bridge import (
    CommandDispatchParams,
    CommandDispatchResult,
    DispatchKind,
    SlashExecParams,
    SlashExecResult,
)
from indus.protocol.errors import (
    SESSION_BUSY,
    SLASH_NOT_OWNED,
    SLASH_WORKER_FAILED,
    UNSUPPORTED_COMMAND,
    ErrorCode,
)
from indus.protocol.methods import ProtocolError
from indus.service import picker_reads
from indus.service.commands.registry import REGISTRY, CommandDef, Surface, command_word
from indus.service.session_service import SessionService, usage_numbers
from indus.service.transcript_view import display_messages

if TYPE_CHECKING:
    from indus.learning.skills.invoke import CommandTable

__all__ = [
    "HANDLERS",
    "MAX_ALIAS_HOPS",
    "MIN_TIMEOUT_S",
    "SlashHandler",
    "SlashHost",
    "dispatch",
    "out",
    "run",
    "running",
    "slash_exec",
]

log = logging.getLogger(__name__)

MIN_TIMEOUT_S = 5.0
MAX_ALIAS_HOPS = 5


class SlashHost(Protocol):
    """What a transport gives the shared commands (see the module notes)."""

    @property
    def scope(self) -> Scope: ...
    @property
    def service(self) -> SessionService: ...
    @property
    def surface(self) -> Surface: ...
    @property
    def timeout(self) -> float:
        """How long a handler may take (the host keeps it at least ``MIN_TIMEOUT_S``)."""
        ...

    @property
    def refused(self) -> Mapping[str, str]:
        """Built-in name → why this host does not run it."""
        ...

    @property
    def quick_exec_refusal(self) -> str | None:
        """Why quick ``exec`` commands do not run here (None: they do)."""
        ...

    def skills(self, session_id: str | None) -> CommandTable | None:
        """The skills and bundles a slash word can run (None when they cannot be listed)."""
        ...

    def queued(self, session_id: str) -> int:
        """Prompts this viewer queued for the session (0 when the host keeps none)."""
        ...

    async def title(self, session_id: str, title: str | None) -> tuple[str | None, bool]:
        """Read (None) or set the title: ``(title, pending)``."""
        ...

    async def undo(self, session_id: str, count: int) -> tuple[int, str]:
        """Take back ``count`` user messages: ``(rows removed, the text to put back)``."""
        ...

    async def branch(self, session_id: str, title: str | None) -> tuple[str, str | None]:
        """A stored copy: ``(child id, child title)``."""
        ...

    async def compress(self, session_id: str, focus: str | None) -> str:
        """Compress now; the message to show."""
        ...

    def stop(self, session_id: str) -> bool:
        """Stop the running turn hard; whether one was running."""
        ...

    def steer(self, session_id: str, text: str) -> bool:
        """Hand ``text`` to the running turn; whether it took it."""
        ...

    async def run_quick(self, session_id: str, command: str) -> tuple[str, str, int]:
        """Run a quick ``exec`` command: ``(stdout, stderr, exit code)``."""
        ...


type SlashHandler = Callable[[Any, str, str], Awaitable[SlashExecResult]]
"""``(host, session id, argument) -> result``; ``host`` is the transport's ``SlashHost``."""


# -- helpers ---------------------------------------------------------------------------------------


def running(service: SessionService, session_id: str) -> bool:
    try:
        return service.agent(session_id).current is not None
    except ProtocolError:
        return False


def out(*lines: str, **extra: Any) -> SlashExecResult:
    return SlashExecResult(output="\n".join(lines), **extra)


# -- the shared handlers ---------------------------------------------------------------------------


async def _title(host: SlashHost, sid: str, arg: str) -> SlashExecResult:
    title, pending = await host.title(sid, arg.strip() or None)
    if not arg.strip():
        return out(f"Title: {title}" if title else "This session has no title yet.")
    note = " (saved after the first reply)" if pending else ""
    return out(f"Title set: {title}{note}")


async def _undo(host: SlashHost, sid: str, arg: str) -> SlashExecResult:
    raw = arg.strip() or "1"
    if not raw.isdigit() or int(raw) < 1:
        return out("Usage: /undo [N]  (how many of your messages to take back, default 1)")
    removed, prefill = await host.undo(sid, int(raw))
    if not removed:
        return out("There is nothing to undo yet.")
    return out(f"Took back {removed} stored messages.", prefill=prefill or None)


async def _retry(host: SlashHost, sid: str, arg: str) -> SlashExecResult:
    removed, prefill = await host.undo(sid, 1)
    if not removed or not prefill.strip():
        return out("There is no message to send again yet.")
    return out("Sending your last message again.", submit=prefill)


async def _branch(host: SlashHost, sid: str, arg: str) -> SlashExecResult:
    child, title = await host.branch(sid, arg.strip() or None)
    shown = title or child
    return out(f"Branched into '{shown}'. The original stays as it was.", switch_to=child)


async def _compress(host: SlashHost, sid: str, arg: str) -> SlashExecResult:
    return out(await host.compress(sid, arg.strip() or None))


async def _stop(host: SlashHost, sid: str, arg: str) -> SlashExecResult:
    return out("Stopped the running reply." if host.stop(sid) else "Nothing was running.")


async def _steer(host: SlashHost, sid: str, arg: str) -> SlashExecResult:
    if not arg.strip():
        return out("Usage: /steer <text>")
    if host.steer(sid, arg):
        return out("The running reply will pick this up after its current step.")
    return out("Nothing is running; sending it as a message.", submit=arg.strip())


async def _status(host: SlashHost, sid: str, arg: str) -> SlashExecResult:
    agent = host.service.agent(sid)
    state = agent.binding.state
    lines = [
        f"Session: {agent.session_id}",
        f"Model: {state.model} ({state.provider})",
        f"Reasoning: {state.reasoning_effort or 'off'}",
        f"Folder: {agent.options.cwd or agent.options.session_fields.get('cwd') or '-'}",
        "A reply is running." if agent.current is not None else "Idle.",
    ]
    queued = host.queued(sid)
    if queued:
        lines.append(f"Queued messages: {queued}")
    return out(*lines)


async def _usage(host: SlashHost, sid: str, arg: str) -> SlashExecResult:
    n = usage_numbers(host.service, sid)
    return out(
        f"Model calls: {n['api_calls']}",
        f"Tokens: {n['input_tokens']} in, {n['output_tokens']} out, "
        f"{n['cache_read_tokens']} from cache",
        f"Cost: {n['cost_label'] or 'unknown'}",
    )


async def _history(host: SlashHost, sid: str, arg: str) -> SlashExecResult:
    service = host.service
    rows = display_messages(await service.store.load(service.current_id(sid)))
    if not rows:
        return out("No messages yet.")
    lines = [
        f"{row.role.value}: {row.text.splitlines()[0][:120] if row.text else ''}" for row in rows
    ]
    return out(*lines[-40:])


async def _help(host: SlashHost, sid: str, arg: str) -> SlashExecResult:
    wanted = arg.strip().lstrip("/").lower()
    lines: list[str] = []
    for cmd in REGISTRY.commands(host.surface):
        if wanted and wanted not in cmd.name:
            continue
        lines.append(f"{cmd.usage():<28} {cmd.description}")
    return out(*(lines or [f"No command matches '{wanted}'."]))


async def _version(host: SlashHost, sid: str, arg: str) -> SlashExecResult:
    try:
        number = metadata.version("indus")
    except metadata.PackageNotFoundError:
        number = "unknown"
    return out(f"Indus Agent {number}")


async def _tools(host: SlashHost, sid: str, arg: str) -> SlashExecResult:
    names = sorted(host.service.agent(sid).tool_names)
    return out(f"{len(names)} tools: " + ", ".join(names) if names else "No tools are enabled.")


HANDLERS: dict[str, SlashHandler] = {
    "title": _title,
    "undo": _undo,
    "retry": _retry,
    "branch": _branch,
    "compress": _compress,
    "stop": _stop,
    "steer": _steer,
    "status": _status,
    "usage": _usage,
    "history": _history,
    "help": _help,
    "version": _version,
    "tools": _tools,
}
"""Canonical command name → the handler every host shares."""


# -- running ---------------------------------------------------------------------------------------


def _refusal(name: str, why: str, code: ErrorCode = SLASH_NOT_OWNED) -> ProtocolError:
    return ProtocolError(code, f"/{name}: {why}", path="command")


async def run(
    host: SlashHost,
    handlers: Mapping[str, SlashHandler],
    cmd: CommandDef,
    session_id: str,
    arg: str,
    *,
    refusal_code: ErrorCode = SLASH_NOT_OWNED,
) -> SlashExecResult:
    """Run built-in ``cmd`` for the session (see the module notes)."""
    why = host.refused.get(cmd.name)
    if why is not None:
        raise _refusal(cmd.name, why, refusal_code)
    handler = handlers.get(cmd.name)
    if handler is None:
        where = (
            "the terminal UI runs it itself"
            if cmd.name in picker_reads.CLIENT_COMMANDS
            else (f"not available in the terminal UI yet (arrives with {cmd.owner})")
        )
        raise ProtocolError(SLASH_NOT_OWNED, f"/{cmd.name}: {where}", path="command")
    if cmd.busy_policy == "reject" and running(host.service, session_id):
        raise ProtocolError(SESSION_BUSY, f"/{cmd.name} waits until the reply finishes")
    limit = host.timeout
    try:
        return await asyncio.wait_for(handler(host, session_id, arg), timeout=limit)
    except ProtocolError:
        raise
    except TimeoutError as exc:
        raise ProtocolError(
            SLASH_WORKER_FAILED, f"/{cmd.name} did not answer within {limit:g} s"
        ) from exc
    except Exception as exc:
        log.error("/%s failed", cmd.name, exc_info=exc)
        raise ProtocolError(
            SLASH_WORKER_FAILED, f"/{cmd.name} failed: {type(exc).__name__}: {exc}"
        ) from exc


def _quick_exec(spec: Mapping[str, Any] | None) -> bool:
    return spec is not None and str(spec.get("type", "")).lower() == "exec"


async def slash_exec(
    host: SlashHost, handlers: Mapping[str, SlashHandler], params: SlashExecParams
) -> SlashExecResult:
    """``slash.exec``: one built-in command line."""
    word, arg = command_word(params.command)
    cmd = REGISTRY.resolve(word)
    if cmd is None or not cmd.offered_on(host.surface):
        quick = await run_blocking(picker_reads.quick_command_table, host.scope, label="quick")
        refusal = host.quick_exec_refusal
        if refusal is not None and _quick_exec(quick.get(word)):
            raise _refusal(word, refusal)
        if word in quick or _is_skill(host, params.session_id, word):
            raise ProtocolError(
                SLASH_NOT_OWNED, f"/{word}: resolve it with command.dispatch", path="command"
            )
        raise ProtocolError(UNSUPPORTED_COMMAND, f"/{word} is not a command", path="command")
    return await run(host, handlers, cmd, params.session_id, arg)


def _is_skill(host: SlashHost, session_id: str | None, word: str) -> bool:
    table = host.skills(session_id)
    return table is not None and word in table.names()


async def dispatch(
    host: SlashHost, handlers: Mapping[str, SlashHandler], params: CommandDispatchParams
) -> CommandDispatchResult:
    """``command.dispatch``: a quick command, a skill or a built-in."""
    name = params.name.strip().lstrip("/").lower()
    arg = params.arg
    quick_table = await run_blocking(picker_reads.quick_command_table, host.scope, label="quick")
    for _hop in range(MAX_ALIAS_HOPS):
        quick = quick_table.get(name)
        if quick is None or REGISTRY.resolve(name) is not None:
            break
        kind = str(quick.get("type", "")).lower()
        if kind == "exec":
            refusal = host.quick_exec_refusal
            if refusal is not None:
                raise _refusal(name, refusal, UNSUPPORTED_COMMAND)
            command = str(quick.get("command") or "")
            stdout, stderr, code = await host.run_quick(params.session_id, command)
            output = "\n".join(part for part in (stdout, stderr) if part)
            if code:
                output = f"{output}\n(exit {code})".strip()
            return CommandDispatchResult(kind=DispatchKind.EXEC, output=output or "(no output)")
        if kind == "alias":
            target = str(quick.get("target") or "").strip().lstrip("/")
            if not target:
                break
            word, _, rest = target.partition(" ")
            if REGISTRY.resolve(word) is not None:
                joined = " ".join(part for part in (rest, arg) if part)
                return CommandDispatchResult(
                    kind=DispatchKind.ALIAS, target=f"/{word} {joined}".strip()
                )
            name, arg = word.lower(), " ".join(part for part in (rest, arg) if part)
            continue
        break
    table = host.skills(params.session_id)
    if table is not None and name in table.names():
        line = f"/{name} {arg}".strip()
        expansion = table.run(line, scope=host.scope, session_id=params.session_id)
        if expansion is None or not expansion.loaded:
            raise ProtocolError(SLASH_WORKER_FAILED, f"/{name} could not be loaded")
        return CommandDispatchResult(kind=DispatchKind.SKILL, message=expansion.text)
    cmd = REGISTRY.resolve(name)
    if cmd is not None and cmd.offered_on(host.surface):
        ran = await run(
            host, handlers, cmd, params.session_id, arg, refusal_code=UNSUPPORTED_COMMAND
        )
        if ran.submit is not None:
            return CommandDispatchResult(
                kind=DispatchKind.SEND, message=ran.submit, output=ran.output
            )
        if ran.prefill is not None:
            return CommandDispatchResult(
                kind=DispatchKind.PREFILL, message=ran.prefill, output=ran.output
            )
        return CommandDispatchResult(kind=DispatchKind.EXEC, output=ran.output)
    raise ProtocolError(
        UNSUPPORTED_COMMAND, f"/{name} is not a quick, skill or built-in command", path="name"
    )
