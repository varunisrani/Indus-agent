"""Slash commands on the bridge (P12-31): ``commands.catalog``, ``slash.exec`` and
``command.dispatch``.

Commands are looked up in the shared registry (``indus.service.commands``) and run **in process**
by the handlers in ``HANDLERS`` below; the bridge never imports ``indus.cli``. Who runs what:

- ``HANDLERS`` — this module (session, control and information commands);
- ``CLIENT`` — the terminal UI itself (screen, pickers, leaving, files on the client's side);
- everything else is answered ``SLASH_NOT_OWNED`` ("not mine"), so a client can tell "run it
  yourself or later" from a handler that failed (``SLASH_WORKER_FAILED``, also used when a handler
  passes its reply timeout: ``Bridge.slash_timeout``, at least ``MIN_TIMEOUT_S``) (R05.12).

A registry entry whose busy policy is ``reject`` is refused with ``SESSION_BUSY`` while a turn
runs. ``command.dispatch`` resolves, in order, a quick command (``exec`` runs through
``shell.exec``'s checks; ``alias`` names another command), a skill or skill bundle (its expansion
is the prompt to send), then a built-in (``exec`` with ``slash.exec``'s output).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from importlib import metadata
from typing import TYPE_CHECKING, Any, cast

from indus.protocol.bridge import (
    CatalogEntry,
    CommandDispatchParams,
    CommandDispatchResult,
    CommandKind,
    CommandRunner,
    CommandsCatalogParams,
    CommandsCatalogResult,
    ConfigKey,
    ConfigSetParams,
    DispatchKind,
    SessionBranchParams,
    SessionCompressParams,
    SessionTitleParams,
    SessionUndoParams,
    SlashExecParams,
    SlashExecResult,
)
from indus.protocol.errors import (
    SESSION_BUSY,
    SLASH_NOT_OWNED,
    SLASH_WORKER_FAILED,
    UNSUPPORTED_COMMAND,
)
from indus.protocol.methods import ProtocolError
from indus.service.commands.registry import REGISTRY, CommandDef, command_word
from indus.service.session_service import SessionService, usage_numbers
from indus.tui_bridge import methods_config, methods_session

if TYPE_CHECKING:
    from indus.learning.skills.invoke import CommandTable
    from indus.tui_bridge.bridge import Bridge
    from indus.tui_bridge.dispatch import MethodTable

__all__ = [
    "CLIENT",
    "HANDLERS",
    "MIN_TIMEOUT_S",
    "SURFACE",
    "quick_commands",
    "register",
    "skill_table",
]

log = logging.getLogger(__name__)

SURFACE = "tui"
MIN_TIMEOUT_S = 5.0
MAX_ALIAS_HOPS = 5
QUICK_KEY = "quick_commands"
CLIENT = frozenset({"new", "clear", "resume", "sessions", "model", "config", "quit", "save"})

type SlashHandler = Callable[[Bridge, SessionService, str, str], Awaitable[SlashExecResult]]


# -- helpers ---------------------------------------------------------------------------------------


def _running(service: SessionService, session_id: str) -> bool:
    try:
        return service.agent(session_id).current is not None
    except ProtocolError:
        return False


def _out(*lines: str, **extra: Any) -> SlashExecResult:
    return SlashExecResult(output="\n".join(lines), **extra)


async def _switch(bridge: Bridge, session_id: str, key: ConfigKey, value: Any) -> Any:
    params = ConfigSetParams(key=key, value=value, session_id=session_id)
    return (await methods_config.set_value(bridge, params)).value


# -- the bridge's own handlers ---------------------------------------------------------------------


async def _title(bridge: Bridge, service: SessionService, sid: str, arg: str) -> SlashExecResult:
    result = await methods_session.title(
        bridge, SessionTitleParams(session_id=sid, title=arg.strip() or None)
    )
    if not arg.strip():
        return _out(f"Title: {result.title}" if result.title else "This session has no title yet.")
    note = " (saved after the first reply)" if result.pending else ""
    return _out(f"Title set: {result.title}{note}")


async def _undo(bridge: Bridge, service: SessionService, sid: str, arg: str) -> SlashExecResult:
    raw = arg.strip() or "1"
    if not raw.isdigit() or int(raw) < 1:
        return _out("Usage: /undo [N]  (how many of your messages to take back, default 1)")
    result = await methods_session.undo(bridge, SessionUndoParams(session_id=sid, count=int(raw)))
    if not result.removed:
        return _out("There is nothing to undo yet.")
    return _out(f"Took back {result.removed} stored messages.", prefill=result.prefill or None)


async def _retry(bridge: Bridge, service: SessionService, sid: str, arg: str) -> SlashExecResult:
    result = await methods_session.undo(bridge, SessionUndoParams(session_id=sid, count=1))
    if not result.removed or not result.prefill.strip():
        return _out("There is no message to send again yet.")
    return _out("Sending your last message again.", submit=result.prefill)


async def _branch(bridge: Bridge, service: SessionService, sid: str, arg: str) -> SlashExecResult:
    child = await methods_session.branch(
        bridge, SessionBranchParams(session_id=sid, title=arg.strip() or None)
    )
    shown = child.title or child.session_id
    return _out(
        f"Branched into '{shown}'. The original stays as it was.", switch_to=child.session_id
    )


async def _compress(bridge: Bridge, service: SessionService, sid: str, arg: str) -> SlashExecResult:
    done = await methods_session.compress(
        bridge, SessionCompressParams(session_id=sid, focus=arg.strip() or None)
    )
    return _out(done.message or f"Compression: {done.status}.")


async def _stop(bridge: Bridge, service: SessionService, sid: str, arg: str) -> SlashExecResult:
    stopped = service.interrupt(sid, hard=True).interrupted
    view = bridge.view(sid)
    if view is not None:
        view.queue.clear()
    return _out("Stopped the running reply." if stopped else "Nothing was running.")


async def _steer(bridge: Bridge, service: SessionService, sid: str, arg: str) -> SlashExecResult:
    if not arg.strip():
        return _out("Usage: /steer <text>")
    if service.steer(sid, arg).queued:
        return _out("The running reply will pick this up after its current step.")
    return _out("Nothing is running; sending it as a message.", submit=arg.strip())


async def _queue(bridge: Bridge, service: SessionService, sid: str, arg: str) -> SlashExecResult:
    view = bridge.watch(sid)
    word, _, rest = arg.strip().partition(" ")
    word = word.lower()
    if word == "list" or not arg.strip():
        if not view.queue:
            return _out("Nothing is queued.")
        return _out(*(f"{n}. {text}" for n, text in enumerate(view.queue, 1)))
    if word == "clear":
        count = len(view.queue)
        view.queue.clear()
        return _out(f"Cleared {count} queued message(s).")
    if word == "rm":
        if not rest.strip().isdigit() or not 1 <= int(rest) <= len(view.queue):
            return _out("Usage: /queue rm N  (N from /queue list)")
        removed = view.queue[int(rest) - 1]
        del view.queue[int(rest) - 1]
        return _out(f"Removed: {removed}")
    text = rest.strip() if word == "add" else arg.strip()
    if not _running(service, sid):
        return _out("Nothing is running; sending it now.", submit=text)
    view.queue.append(text)
    return _out(f"Queued ({len(view.queue)} waiting).")


async def _status(bridge: Bridge, service: SessionService, sid: str, arg: str) -> SlashExecResult:
    agent = service.agent(sid)
    state = agent.binding.state
    lines = [
        f"Session: {agent.session_id}",
        f"Model: {state.model} ({state.provider})",
        f"Reasoning: {state.reasoning_effort or 'off'}",
        f"Folder: {agent.options.cwd or agent.options.session_fields.get('cwd') or '-'}",
        "A reply is running." if agent.current is not None else "Idle.",
    ]
    view = bridge.view(sid)
    if view is not None and view.queue:
        lines.append(f"Queued messages: {len(view.queue)}")
    return _out(*lines)


async def _usage(bridge: Bridge, service: SessionService, sid: str, arg: str) -> SlashExecResult:
    n = usage_numbers(service, sid)
    return _out(
        f"Model calls: {n['api_calls']}",
        f"Tokens: {n['input_tokens']} in, {n['output_tokens']} out, "
        f"{n['cache_read_tokens']} from cache",
        f"Cost: {n['cost_label'] or 'unknown'}",
    )


async def _history(bridge: Bridge, service: SessionService, sid: str, arg: str) -> SlashExecResult:
    rows = methods_session.display_messages(await service.store.load(service.current_id(sid)))
    if not rows:
        return _out("No messages yet.")
    lines = [
        f"{row.role.value}: {row.text.splitlines()[0][:120] if row.text else ''}" for row in rows
    ]
    return _out(*lines[-40:])


async def _help(bridge: Bridge, service: SessionService, sid: str, arg: str) -> SlashExecResult:
    wanted = arg.strip().lstrip("/").lower()
    lines: list[str] = []
    for cmd in REGISTRY.commands(SURFACE):
        if wanted and wanted not in cmd.name:
            continue
        lines.append(f"{cmd.usage():<28} {cmd.description}")
    return _out(*(lines or [f"No command matches '{wanted}'."]))


async def _version(bridge: Bridge, service: SessionService, sid: str, arg: str) -> SlashExecResult:
    try:
        number = metadata.version("indus")
    except metadata.PackageNotFoundError:
        number = "unknown"
    return _out(f"Indus Agent {number}")


async def _yolo(bridge: Bridge, service: SessionService, sid: str, arg: str) -> SlashExecResult:
    from indus.safety.approvals.decision import APPROVALS, session_key

    enabled = not APPROVALS.auto_approve(session_key(bridge.scope, service.agent(sid).session_id))
    await _switch(bridge, sid, ConfigKey.YOLO, enabled)
    if enabled:
        return _out("Auto-approve is ON for this session; never-allowed commands stay blocked.")
    return _out("Auto-approve is off: risky commands ask first again.")


async def _reasoning(
    bridge: Bridge, service: SessionService, sid: str, arg: str
) -> SlashExecResult:
    if not arg.strip():
        level = service.agent(sid).binding.state.reasoning_effort or "off"
        return _out(f"Reasoning effort: {level}.")
    value = await _switch(bridge, sid, ConfigKey.REASONING, arg.strip().split()[0])
    return _out(f"Reasoning effort: {value}.")


async def _fast(bridge: Bridge, service: SessionService, sid: str, arg: str) -> SlashExecResult:
    word = arg.strip().split()[0] if arg.strip() else "status"
    if word == "status":
        live = methods_config.session_settings(bridge, service, sid)
        return _out(f"Fast mode: {live.get('fast') or 'normal'}.")
    value = await _switch(bridge, sid, ConfigKey.FAST, word)
    return _out(f"Fast mode {value} for this session.")


async def _tools(bridge: Bridge, service: SessionService, sid: str, arg: str) -> SlashExecResult:
    names = sorted(service.agent(sid).tool_names)
    return _out(f"{len(names)} tools: " + ", ".join(names) if names else "No tools are enabled.")


HANDLERS: dict[str, SlashHandler] = {
    "title": _title,
    "undo": _undo,
    "retry": _retry,
    "branch": _branch,
    "compress": _compress,
    "stop": _stop,
    "steer": _steer,
    "queue": _queue,
    "status": _status,
    "usage": _usage,
    "history": _history,
    "help": _help,
    "version": _version,
    "yolo": _yolo,
    "reasoning": _reasoning,
    "fast": _fast,
    "tools": _tools,
}
"""Canonical command name → the bridge's handler."""


# -- quick commands and skills ---------------------------------------------------------------------


def quick_commands(bridge: Bridge) -> dict[str, Mapping[str, Any]]:
    """The user's ``quick_commands`` table, keyed by lowercase name."""
    raw = methods_config.read_setting(bridge.scope, QUICK_KEY)
    if not isinstance(raw, Mapping):
        return {}
    table: dict[str, Mapping[str, Any]] = {}
    for name, spec in cast(Mapping[object, object], raw).items():
        if isinstance(name, str) and name.strip() and isinstance(spec, Mapping):
            table[name.strip().lstrip("/").lower()] = cast(Mapping[str, Any], spec)
    return table


def skill_table(bridge: Bridge, session_id: str | None) -> CommandTable | None:
    """The skills and bundles a slash word can run here (None when they cannot be listed)."""
    try:
        from indus.learning.skills.bundles import bundle_expanders
        from indus.learning.skills.invoke import CommandTable, skill_command_names
        from indus.learning.skills.resolver import load_catalog

        catalog = load_catalog(bridge.scope, platform=SURFACE)
        return CommandTable(
            skills=skill_command_names(catalog), bundles=bundle_expanders(bridge.scope, session_id)
        )
    except Exception:
        log.warning("skills could not be listed", exc_info=True)
        return None


def _runner(cmd: CommandDef) -> CommandRunner:
    if cmd.name in HANDLERS:
        return CommandRunner.SERVER
    if cmd.name in CLIENT:
        return CommandRunner.CLIENT
    return CommandRunner.LATER


# -- handlers --------------------------------------------------------------------------------------


async def catalog(bridge: Bridge, params: CommandsCatalogParams) -> CommandsCatalogResult:
    entries = [
        CatalogEntry(
            name=cmd.name,
            aliases=list(cmd.aliases),
            description=cmd.description,
            category=cmd.category,
            args_hint=cmd.args_hint,
            kind=CommandKind.BUILTIN,
            runs=_runner(cmd),
        )
        for cmd in REGISTRY.commands(SURFACE)
    ]
    taken = {key for cmd in REGISTRY for key in cmd.keys}
    for name, spec in sorted(quick_commands(bridge).items()):
        if name in taken:
            continue
        kind = str(spec.get("type", "exec"))
        entries.append(
            CatalogEntry(
                name=name,
                description=str(spec.get("description") or f"quick command ({kind})"),
                category="custom",
                kind=CommandKind.QUICK,
                runs=CommandRunner.SERVER,
            )
        )
        taken.add(name)
    table = skill_table(bridge, params.session_id)
    if table is not None:
        for name, note in sorted(table.names().items()):
            if name in taken:
                continue
            entries.append(
                CatalogEntry(
                    name=name,
                    description=note,
                    category="custom",
                    kind=CommandKind.SKILL,
                    runs=CommandRunner.SERVER,
                )
            )
    return CommandsCatalogResult(commands=entries)


async def _run(
    bridge: Bridge, service: SessionService, cmd: CommandDef, session_id: str, arg: str
) -> SlashExecResult:
    handler = HANDLERS.get(cmd.name)
    if handler is None:
        where = (
            "the terminal UI runs it itself"
            if cmd.name in CLIENT
            else (f"not available in the terminal UI yet (arrives with {cmd.owner})")
        )
        raise ProtocolError(SLASH_NOT_OWNED, f"/{cmd.name}: {where}", path="command")
    if cmd.busy_policy == "reject" and _running(service, session_id):
        raise ProtocolError(SESSION_BUSY, f"/{cmd.name} waits until the reply finishes")
    limit = max(MIN_TIMEOUT_S, bridge.slash_timeout)
    try:
        return await asyncio.wait_for(handler(bridge, service, session_id, arg), timeout=limit)
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


async def slash_exec(bridge: Bridge, params: SlashExecParams) -> SlashExecResult:
    service = await bridge.service()
    word, arg = command_word(params.command)
    cmd = REGISTRY.resolve(word)
    if cmd is None or not cmd.offered_on(SURFACE):
        if word in quick_commands(bridge) or _is_skill(bridge, params.session_id, word):
            raise ProtocolError(
                SLASH_NOT_OWNED, f"/{word}: resolve it with command.dispatch", path="command"
            )
        raise ProtocolError(UNSUPPORTED_COMMAND, f"/{word} is not a command", path="command")
    return await _run(bridge, service, cmd, params.session_id, arg)


def _is_skill(bridge: Bridge, session_id: str | None, word: str) -> bool:
    table = skill_table(bridge, session_id)
    return table is not None and word in table.names()


async def dispatch(bridge: Bridge, params: CommandDispatchParams) -> CommandDispatchResult:
    from indus.tui_bridge.methods_shell import run_host_command

    service = await bridge.service()
    name = params.name.strip().lstrip("/").lower()
    arg = params.arg
    for _hop in range(MAX_ALIAS_HOPS):
        quick = quick_commands(bridge).get(name)
        if quick is None or REGISTRY.resolve(name) is not None:
            break
        kind = str(quick.get("type", "")).lower()
        if kind == "exec":
            command = str(quick.get("command") or "")
            ran = await run_host_command(bridge, command, session_id=params.session_id)
            output = "\n".join(part for part in (ran.stdout, ran.stderr) if part)
            if ran.code:
                output = f"{output}\n(exit {ran.code})".strip()
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
    table = skill_table(bridge, params.session_id)
    if table is not None and name in table.names():
        line = f"/{name} {arg}".strip()
        expansion = table.run(line, scope=bridge.scope, session_id=params.session_id)
        if expansion is None or not expansion.loaded:
            raise ProtocolError(SLASH_WORKER_FAILED, f"/{name} could not be loaded")
        return CommandDispatchResult(kind=DispatchKind.SKILL, message=expansion.text)
    cmd = REGISTRY.resolve(name)
    if cmd is not None and cmd.offered_on(SURFACE):
        ran = await _run(bridge, service, cmd, params.session_id, arg)
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


def register(table: MethodTable) -> None:
    table.add("commands.catalog", catalog)
    table.add("slash.exec", slash_exec)
    table.add("command.dispatch", dispatch)
