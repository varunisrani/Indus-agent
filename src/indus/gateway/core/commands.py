"""Slash commands typed in a chat (P15-13).

A command word resolves through the shared slash registry (``indus.service.commands``): names and
aliases first, then the user's quick commands, plugin commands and skills by exact name, then a
unique prefix among the gateway's registry names (an ambiguous prefix gets a "which one?" answer,
an unknown word a short hint). The admin tiers (P15-10) gate every kind of command; plain chat is
never gated. ``command:<name>`` hooks may deny, answer or rewrite a command before it runs.

Handlers are looked up by name in two tables, never chosen by if-chains:

- ``ANYTIME`` — usable idle or while a reply runs (status, approve, deny, help, commands, yolo,
  verbose, usage, bg, btw, platform, version, subgoal, heartbeat);
- ``IDLE_ONLY`` — new/reset, resume, sessions, title, model, sethome, whoami, retry, undo,
  compress, approvals, personality, skills, memory, reload-mcp, moa, suggestions, blueprint, and
  the idle forms of stop, queue, steer, goal and loop.

``MID_RUN`` holds the variants the busy guard (P15-12) runs while a reply is in progress: stop,
new, queue, steer, goal, loop. Another command runs mid-reply only when the registry's busy policy
allows it and it is in ``ANYTIME``; anything else gets one fixed answer.

A handler answers text, ``None`` (nothing to say) or ``Rewrite(text)``: the message continues to
the agent with that text (``/queue`` and ``/steer`` while idle, ``/retry``, skills, goal
objectives). ``/new`` and ``/undo`` ask for a numbered confirmation while
``approvals.destructive_slash_confirm`` is on, ``/reload-mcp`` while
``approvals.mcp_reload_confirm`` is on (``PendingConfirms``, an admission interceptor).

Quick commands of type ``exec`` run in a child environment from the env builder (no provider keys,
R04.28), are stopped after 30 s, and their output is redacted. Sudo and secret prompts are never
answered in chat: the gateway declares only approval and clarify requests answerable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import signal
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from typing import TYPE_CHECKING, Any, Final, cast

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.config import loader as config_loader
from indus.foundation.config import writer as config_writer
from indus.foundation.digits import is_digits
from indus.foundation.scope import Scope
from indus.gateway.core.events import InboundEvent, Source
from indus.gateway.core.slash_access import SlashPolicy, policy_for, scope_of, tier_label
from indus.protocol.base import ProtocolModel
from indus.protocol.methods import ProtocolError, SessionCreateParams
from indus.protocol.server_requests import ApprovalAnswer, ApprovalChoice
from indus.service.commands.registry import REGISTRY, CommandDef
from indus.state import gateway_routes as routes
from indus.state import rewind, titles

if TYPE_CHECKING:
    from indus.gateway.core.admission import AdmissionPipeline

__all__ = [
    "ANYTIME",
    "BUSY_REJECT",
    "EXEC_TIMEOUT_S",
    "IDLE_ONLY",
    "MID_RUN",
    "NOT_IN_CHAT",
    "ChatCommands",
    "CommandContext",
    "PendingConfirms",
    "Rewrite",
    "run_exec_quick",
]

log = logging.getLogger(__name__)

SURFACE: Final = "gateway"
EXEC_TIMEOUT_S: Final = 30.0
MAX_ALIAS_HOPS: Final = 5
CONFIRM_TTL_S: Final = 300.0
OUTPUT_CAP: Final = 16_000
TOOL_MODES: Final = ("off", "new", "all", "verbose")
PLATFORM_PINGS: Final = frozenset({"start"})
"""Words a platform sends on its own (Telegram's bot-start): answered with silence."""

BUSY_REJECT: Final = (
    "/{name} can't run while I'm replying. Wait for the reply, or send /stop first."
)
NOT_IN_CHAT: Final = "/{name} isn't available in chat yet; use the terminal app for it."
TERMINAL_ONLY: Final = "/{name} only works in the terminal app."
UNKNOWN: Final = "I don't know /{name}. Send /help to see what I can do here."
DENIED: Final = "You can't run /{name} here. Send /help to see what you can run."


@dataclass(frozen=True)
class Rewrite:
    """Continue to the agent with ``text`` instead of answering."""

    text: str


type Reply = str | Rewrite | None


@dataclass
class CommandContext:
    pipeline: AdmissionPipeline
    event: InboundEvent
    key: str
    name: str
    args: str
    busy: bool = False

    @property
    def source(self) -> Source:
        return self.event.source

    @property
    def scope(self) -> Scope:
        return self.pipeline.scope

    @property
    def platform(self) -> str:
        return str(self.source.platform).lower()


type Handler = Callable[[CommandContext], Awaitable[Reply]]
type PluginCommand = Callable[[str, Mapping[str, Any]], Awaitable[str | None]]
type CommandHooks = Callable[[str, Mapping[str, Any]], Awaitable[Sequence[Any]]]


# -- confirmations ---------------------------------------------------------------------------------


@dataclass
class _Confirm:
    user_id: str
    run: Callable[[str], Awaitable[str | None]]
    switch_key: str | None
    expires_at: float


class PendingConfirms:
    """Numbered confirmations for destructive commands: 1 go ahead, 2 always, 3 cancel."""

    def __init__(self, pipeline: AdmissionPipeline) -> None:
        self.pipeline = pipeline
        self._pending: dict[str, _Confirm] = {}

    def _now(self) -> float:
        return self.pipeline.clock.monotonic()

    def waiting(self, key: str) -> bool:
        pending = self._pending.get(key)
        return pending is not None and pending.expires_at > self._now()

    def ask(
        self,
        ctx: CommandContext,
        *,
        question: str,
        run: Callable[[str], Awaitable[str | None]],
        switch_key: str | None,
    ) -> str:
        self._pending[ctx.key] = _Confirm(
            str(ctx.source.user_id or ""), run, switch_key, self._now() + CONFIRM_TTL_S
        )
        return f"{question}\nReply 1 to go ahead, 2 to go ahead and stop asking, or 3 to cancel."

    async def intercept(self, event: InboundEvent, key: str) -> str | None:
        pending = self._pending.get(key)
        if pending is None:
            return None
        if pending.expires_at <= self._now():
            del self._pending[key]
            return None
        if str(event.source.user_id or "") != pending.user_id:
            return None
        answer = event.text.strip().rstrip(".").lower()
        choice = {"1": "once", "2": "always", "3": "cancel"}.get(answer)
        if choice is None:
            return None
        del self._pending[key]
        if choice == "cancel":
            return "Cancelled."
        note = ""
        if choice == "always" and pending.switch_key:
            section, _, name = pending.switch_key.partition(".")
            try:
                await run_blocking(
                    config_writer.save,
                    self.pipeline.scope,
                    {section: {name: False}},
                    label="settings write",
                )
                note = f"\n(I won't ask again: {pending.switch_key} is now false.)"
            except Exception as exc:
                note = f"\n(Could not save {pending.switch_key}: {exc})"
        result = await pending.run(choice)
        return f"{result or ''}{note}".strip()


# -- quick commands --------------------------------------------------------------------------------


def _config_view(scope: Scope) -> Any:
    return scope.config if scope.config is not None else config_loader.load(scope)


def quick_commands(scope: Scope) -> dict[str, Mapping[str, Any]]:
    """The user's ``quick_commands`` table by lowercase name."""
    try:
        raw = _config_view(scope).get("quick_commands")
    except Exception:
        return {}
    if not isinstance(raw, Mapping):
        return {}
    table: dict[str, Mapping[str, Any]] = {}
    for name, spec in cast(Mapping[object, object], raw).items():
        if isinstance(name, str) and name.strip() and isinstance(spec, Mapping):
            table[name.strip().lstrip("/").lower()] = cast(Mapping[str, Any], spec)
    return table


async def run_exec_quick(
    scope: Scope, command: str, *, clock: Clock, timeout: float = EXEC_TIMEOUT_S
) -> str:
    """Run a quick ``exec`` command: env-builder environment, ``timeout`` limit, redacted output."""
    from indus.safety.child_env import child_env
    from indus.safety.redaction import redact_output

    env = child_env(scope)
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except Exception as exc:
        return f"The command could not start: {exc}"
    talk = asyncio.ensure_future(proc.communicate())
    timer = asyncio.ensure_future(clock.sleep(timeout))
    try:
        await asyncio.wait({talk, timer}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        timer.cancel()
    if not talk.done():
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGKILL)
            else:  # pragma: no cover - not POSIX
                proc.kill()
        with contextlib.suppress(Exception):
            await talk
        return f"The command ran longer than {timeout:g} s and was stopped."
    out, err = talk.result()
    text = (out or err or b"").decode("utf-8", errors="replace").strip()
    if len(text) > OUTPUT_CAP:
        text = text[:OUTPUT_CAP] + "\n[... output cut]"
    if proc.returncode:
        text = f"{text}\n(exit {proc.returncode})".strip()
    return redact_output(text, command=command, force=True, scope=scope) or "(no output)"


# -- helpers ---------------------------------------------------------------------------------------


def _first(args: str) -> tuple[str, str]:
    word, _, rest = args.strip().partition(" ")
    return word.lower(), rest.strip()


async def _title_of(pipeline: AdmissionPipeline, session_id: str) -> str | None:
    try:
        return await run_blocking(titles.get_title, pipeline.store, session_id, label="title")
    except Exception:
        return None


def _model_label(pipeline: AdmissionPipeline, session_id: str | None) -> str:
    if session_id:
        try:
            state = pipeline.service.agent(session_id).binding.state
            return f"{state.model} ({state.provider})"
        except Exception:
            pass
    route = pipeline.service.route
    return f"{route.model} ({route.provider})"


async def _open_approvals(pipeline: AdmissionPipeline, session_id: str | None) -> list[Any]:
    if not session_id or session_id not in pipeline.service.attached():
        return []
    described = await pipeline.service.resume(session_id)
    found: list[ProtocolModel] = list(described.pending_requests)
    return [r for r in found if getattr(r, "kind", "") == "approval"]


async def _rewind(pipeline: AdmissionPipeline, session_id: str, count: int) -> rewind.RewindResult:
    have = await run_blocking(
        rewind.recent_user_messages, pipeline.store, session_id, count, label="undo check"
    )
    if not have:
        return rewind.RewindResult(0, "", None, 0)
    return await run_blocking(
        rewind.rewind, pipeline.store, session_id, -min(count, len(have)), None, label="undo"
    )


def _preview(text: str, limit: int = 60) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


# -- any-time handlers -----------------------------------------------------------------------------


async def _status(ctx: CommandContext) -> Reply:
    p = ctx.pipeline
    sid = await p.session_id_for(ctx.key)
    lines: list[str] = []
    if sid is None:
        lines.append("No conversation yet: send a message to start one.")
    else:
        title = await _title_of(p, sid)
        lines.append(f"Conversation: {title or sid}")
        lines.append(f"Model: {_model_label(p, sid)}")
    lines.append("Replying now." if p.busy(ctx.key) else "Idle.")
    waiting = len(await _open_approvals(p, sid))
    if waiting:
        lines.append(f"Waiting for your approval: {waiting} (/approve or /deny)")
    queued = p.pending.depth(ctx.key)
    if queued:
        lines.append(f"Queued messages: {queued}")
    lines.append(f"Messages sent while I reply: {p.settings().busy_mode}")
    return "\n".join(lines)


_APPROVE_WORDS: Final[Mapping[str, ApprovalChoice]] = {
    "": ApprovalChoice.ONCE,
    "once": ApprovalChoice.ONCE,
    "session": ApprovalChoice.SESSION,
    "always": ApprovalChoice.ALWAYS,
}
_APPROVED: Final[Mapping[ApprovalChoice, str]] = {
    ApprovalChoice.ONCE: "Approved.",
    ApprovalChoice.SESSION: "Approved for the rest of this conversation.",
    ApprovalChoice.ALWAYS: "Approved, and I won't ask about this command again.",
}


async def _refusal(ctx: CommandContext, session_id: str) -> tuple[bool, Reply]:
    """Who may answer (P15-22): never a bot (no reply at all), else the person whose message
    started the turn or an admin of the chat's tier list. ``(refused, reply)``. The chat
    prompts' front door applies the same rule; this covers a command that reaches the handler
    without passing it."""
    from indus.gateway.core.prompts import may_answer, requester_of
    from indus.service import approval_copy

    if ctx.source.is_bot:
        return True, None
    asker = await ctx.pipeline.origin_source(session_id)
    if may_answer(ctx.pipeline, ctx.source, requester_of(asker)):
        return False, None
    return True, approval_copy.NOT_REQUESTER


async def _approve(ctx: CommandContext) -> Reply:
    p = ctx.pipeline
    word = ctx.args.strip().lower()
    if word not in _APPROVE_WORDS and word != "all":
        return "Usage: /approve, /approve session, /approve always or /approve all"
    sid = await p.session_id_for(ctx.key)
    waiting = await _open_approvals(p, sid)
    if not sid or not waiting:
        return "Nothing is waiting for your approval."
    refused, answer = await _refusal(ctx, sid)
    if refused:
        return answer
    choice = ApprovalChoice.ONCE if word == "all" else _APPROVE_WORDS[word]
    targets = waiting if word == "all" else waiting[:1]
    done = 0
    for request in targets:
        if choice not in request.choices:
            return f"'{word}' isn't offered for this command. Reply /approve or /deny."
        try:
            p.service.answer_server_request(sid, request.request_id, ApprovalAnswer(choice=choice))
            done += 1
        except ProtocolError:
            continue
    if not done:
        return "That request is no longer waiting."
    if word == "all":
        return f"Approved {done} waiting command(s)."
    return _APPROVED[choice]


async def _deny(ctx: CommandContext) -> Reply:
    p = ctx.pipeline
    sid = await p.session_id_for(ctx.key)
    waiting = await _open_approvals(p, sid)
    if not sid or not waiting:
        return "Nothing is waiting for your approval."
    refused, answer = await _refusal(ctx, sid)
    if refused:
        return answer
    reason = ctx.args.strip() or None
    try:
        p.service.answer_server_request(
            sid,
            waiting[0].request_id,
            ApprovalAnswer(choice=ApprovalChoice.DENY, reason=reason),
        )
    except ProtocolError:
        return "That request is no longer waiting."
    return "Denied." if reason is None else f"Denied: {reason}"


async def _help(ctx: CommandContext) -> Reply:
    commands = ctx.pipeline.commands
    wanted = ctx.args.strip().lstrip("/").lower()
    lines = [
        f"{cmd.usage()} - {cmd.description}"
        for cmd in commands.runnable(ctx.source)
        if not wanted or wanted in cmd.name
    ]
    policy = commands.policy(ctx.source)
    for name, spec in sorted(quick_commands(ctx.scope).items()):
        if policy.can_run(ctx.source.user_id, name) and (not wanted or wanted in name):
            lines.append(f"/{name} - {spec.get('description') or 'quick command'}")
    if not lines:
        return f"No command matches '{wanted}'." if wanted else "No commands are open to you here."
    return "Commands you can run here:\n" + "\n".join(lines)


async def _commands(ctx: CommandContext) -> Reply:
    commands = ctx.pipeline.commands
    names = [f"/{cmd.name}" for cmd in commands.runnable(ctx.source)]
    policy = commands.policy(ctx.source)
    names += [
        f"/{name}"
        for name in sorted(quick_commands(ctx.scope))
        if policy.can_run(ctx.source.user_id, name)
    ]
    return " ".join(names) if names else "No commands are open to you here."


async def _yolo(ctx: CommandContext) -> Reply:
    from indus.safety.approvals.decision import APPROVALS, session_key
    from indus.state import sessions

    p = ctx.pipeline
    sid = await p.session_for(ctx.key, ctx.source)
    key = session_key(p.scope, sid)
    enabled = not APPROVALS.auto_approve(key)
    store = p.store

    def persist(on: bool) -> object:
        return sessions.patch_model_config(store, sid, {sessions.YOLO_KEY: bool(on)})

    APPROVALS.set_auto_approve(key, enabled, persist=persist)
    if enabled:
        return "Auto-approve is ON for this conversation; never-allowed commands stay blocked."
    return "Auto-approve is off: risky commands ask first again."


async def _verbose(ctx: CommandContext) -> Reply:
    modes = ctx.pipeline.progress_modes
    word = ctx.args.strip().lower()
    current = modes.get(ctx.key, "all")
    if word and word not in TOOL_MODES:
        return "Usage: /verbose [off|new|all|verbose]"
    chosen = word or TOOL_MODES[(TOOL_MODES.index(current) + 1) % len(TOOL_MODES)]
    modes[ctx.key] = chosen
    return f"Tool activity in this chat: {chosen}."


async def _usage(ctx: CommandContext) -> Reply:
    p = ctx.pipeline
    sid = await p.session_id_for(ctx.key)
    if not sid or sid not in p.service.attached():
        return "No usage yet in this conversation."
    numbers = p.service.agent(sid).usage.snapshot().as_dict()
    return "\n".join(
        (
            f"Model calls: {numbers.get('api_calls', 0)}",
            f"Tokens: {numbers.get('input_tokens', 0)} in, {numbers.get('output_tokens', 0)} out, "
            f"{numbers.get('cache_read_tokens', 0)} from cache",
            f"Cost: {numbers.get('cost_label') or 'unknown'}",
        )
    )


async def _bg(ctx: CommandContext) -> Reply:
    prompt = ctx.args.strip()
    if not prompt:
        return "Usage: /bg <prompt>"
    task_id = f"bg_{secrets.token_hex(3)}"
    p = ctx.pipeline
    p.spawn(
        _run_background(p, ctx.source, prompt, task_id, ctx.event.message_id),
        name=f"background {task_id}",
    )
    return f"Started background task {task_id}: {_preview(prompt)}. I'll post the result here."


async def _run_background(
    p: AdmissionPipeline, source: Source, prompt: str, task_id: str, reply_to: str | None
) -> None:
    sid: str | None = None
    text = ""
    ok = False
    try:
        sid = await p.create_session(
            SessionCreateParams(toolsets=p.toolsets_for(str(source.platform).lower()))
        )
        ok, text = await p.side_turn(sid, prompt, source)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("background task %s failed", task_id, exc_info=True)
        text = getattr(exc, "message", None) or type(exc).__name__
    finally:
        if sid is not None:
            with contextlib.suppress(Exception):
                await p.close_session(sid)
    head = f"[{task_id}] Done:" if ok else f"[{task_id}] Failed:"
    await p.reply(source, f"{head}\n{text}", reply_to=reply_to, kind="final")


def _automation(name: str) -> Handler:
    async def run(ctx: CommandContext) -> Reply:
        p = ctx.pipeline
        if p.automation is None:
            return f"/{name} isn't running in this gateway."
        sid = await p.session_for(ctx.key, ctx.source)
        answer = await p.automation.command(name, sid, ctx.args)
        text = "\n".join(answer.lines)
        if answer.submit and not ctx.busy:
            if text:
                await p.reply(ctx.source, text, reply_to=ctx.event.message_id)
            return Rewrite(answer.submit)
        return text or None

    return run


def _wait_words(seconds: float) -> str:
    if seconds < 90:
        return f"{max(0, round(seconds))} s"
    return f"{round(seconds / 60)} min"


def _reconnect_lines(p: AdmissionPipeline) -> list[str]:
    """The platforms the reconnect watcher (P15-29) holds: retrying, held, down."""
    watcher = p.reconnect
    if watcher is None:
        return []
    now = watcher.clock.monotonic()
    retrying: list[str] = []
    held: list[str] = []
    down: list[str] = []
    for name, health in sorted(watcher.snapshot().items()):
        if health.state == "fatal":
            down.append(f"{name} ({health.error or health.error_code or 'cannot connect'})")
            continue
        notes = [f"{health.attempts} tries"] if health.attempts else []
        if health.state == "retrying":
            notes.append(f"next in {_wait_words(health.next_retry - now)}")
        if health.needs_attention:
            notes.append("needs attention")
        entry = f"{name} ({', '.join(notes)})" if notes else name
        (retrying if health.state == "retrying" else held).append(entry)
    lines: list[str] = []
    if retrying:
        lines.append(f"Reconnecting: {'; '.join(retrying)}")
    if held:
        lines.append(f"Reconnect paused: {'; '.join(held)}")
    if down:
        lines.append(f"Down (needs a config fix): {'; '.join(down)}")
    return lines


async def _platform(ctx: CommandContext) -> Reply:
    p = ctx.pipeline
    action, target = _first(ctx.args)
    action = action or "list"
    names = sorted(p.adapters)
    if action == "list":
        live = [n for n in names if n not in p.paused_platforms]
        lines = [f"Connected: {', '.join(live) or 'none'}"]
        if p.paused_platforms:
            lines.append(f"Paused (messages ignored): {', '.join(sorted(p.paused_platforms))}")
        lines += _reconnect_lines(p)
        return "\n".join(lines)
    if action not in ("pause", "resume"):
        return "Usage: /platform list, /platform pause <name> or /platform resume <name>"
    target = target.lower()
    if not target:
        return f"Usage: /platform {action} <name>"
    watcher = p.reconnect
    if target not in p.adapters and watcher is not None and watcher.get(target) is not None:
        if action == "pause":
            if watcher.pause(target):
                return (
                    f"Stopped trying to reconnect {target}. /platform resume {target} tries again."
                )
            return f"{target} is not being retried right now."
        if watcher.resume(target):
            return f"Trying to reconnect {target} now."
        return f"{target} isn't paused."
    if target not in p.adapters:
        return f"No platform called {target} is connected."
    if action == "pause":
        if target in p.paused_platforms:
            return f"{target} is already paused."
        p.paused_platforms.add(target)
        return (
            f"Paused {target}: I stay connected but ignore its messages. Send "
            f"/platform resume {target} from another platform (or restart the gateway) to undo."
        )
    if target not in p.paused_platforms:
        return f"{target} isn't paused."
    p.paused_platforms.discard(target)
    return f"Resumed {target}."


async def _version(ctx: CommandContext) -> Reply:
    try:
        number = metadata.version("indus")
    except metadata.PackageNotFoundError:
        number = "unknown"
    return f"Indus Agent {number}"


# -- idle-only handlers ----------------------------------------------------------------------------


async def _do_new(ctx: CommandContext) -> str:
    p = ctx.pipeline
    p.pending.clear(ctx.key)
    p.once_next.pop(ctx.key, None)
    await p.new_session(ctx.key, ctx.source)
    return "Started a new conversation. The earlier one is kept; /sessions lists it."


async def _new(ctx: CommandContext) -> Reply:
    if ctx.pipeline.settings().destructive_confirm:

        async def run(_choice: str) -> str | None:
            return await _do_new(ctx)

        return ctx.pipeline.commands.confirms.ask(
            ctx,
            question="Start a new conversation? This one is closed (you can /resume it).",
            run=run,
            switch_key="approvals.destructive_slash_confirm",
        )
    return await _do_new(ctx)


def _is_admin(ctx: CommandContext) -> bool:
    policy = ctx.pipeline.commands.policy(ctx.source)
    return policy.enabled and policy.is_admin(ctx.source.user_id)


async def _own_sessions(ctx: CommandContext) -> list[str]:
    p = ctx.pipeline
    ids = p.history(ctx.key)
    entry = await run_blocking(routes.get, p.store, ctx.key, label="gateway route")
    if entry is not None:
        for sid in (entry.session_id, entry.prev_session_id):
            if sid and sid not in ids:
                ids.append(sid)
    return ids


async def _session_rows(
    ctx: CommandContext, *, cross: bool, query: str, limit: int = 10
) -> list[tuple[str, str | None]]:
    p = ctx.pipeline
    needle = query.strip().lower()
    if cross:
        from indus.state.listing import ListFilters, list_sessions

        found = await run_blocking(
            lambda: list_sessions(p.store, ListFilters(query=needle or None), limit=limit),
            label="session list",
        )
        return [(row.id, row.title) for row in found]
    rows: list[tuple[str, str | None]] = []
    for sid in await _own_sessions(ctx):
        title = await _title_of(p, sid)
        if needle and needle not in sid.lower() and needle not in (title or "").lower():
            continue
        rows.append((sid, title))
    return rows[:limit]


async def _sessions(ctx: CommandContext) -> Reply:
    words = ctx.args.split()
    cross = bool(words) and words[0].lower() == "all"
    if cross:
        words = words[1:]
    if words and words[0].lower() == "search":
        words = words[1:]
        if not words:
            return "Usage: /sessions [all] [search <words>]"
    notice = ""
    if cross and not _is_admin(ctx):
        cross = False
        notice = "Only an admin can list other chats' conversations; here are this chat's.\n"
    rows = await _session_rows(ctx, cross=cross, query=" ".join(words))
    if not rows:
        return f"{notice}No conversations found."
    current = ctx.pipeline.cached_session(ctx.key)
    lines = [
        f"{n}. {title or '(untitled)'} - {sid}{' (current)' if sid == current else ''}"
        for n, (sid, title) in enumerate(rows, 1)
    ]
    return notice + "\n".join(lines) + "\nSwitch with /resume <title or id>."


async def _resume(ctx: CommandContext) -> Reply:
    wanted = ctx.args.strip()
    if not wanted:
        return await _sessions(ctx)
    rows = await _session_rows(ctx, cross=False, query="", limit=50)
    match = _match_session(rows, wanted)
    if match is None and _is_admin(ctx):
        match = _match_session(await _session_rows(ctx, cross=True, query=wanted), wanted)
    if match is None:
        return f"No conversation of this chat matches '{wanted}'. /sessions lists them."
    sid, title = match
    p = ctx.pipeline
    if sid == p.cached_session(ctx.key):
        return "That is already the current conversation."
    try:
        await p.switch_session(ctx.key, ctx.source, sid)
    except ProtocolError as exc:
        return f"Could not resume it: {exc.message}"
    return f"Resumed '{title or sid}'."


def _match_session(
    rows: Sequence[tuple[str, str | None]], wanted: str
) -> tuple[str, str | None] | None:
    low = wanted.lower()
    for sid, title in rows:
        if sid == wanted or (title or "").lower() == low:
            return sid, title
    hits = [
        (sid, title)
        for sid, title in rows
        if sid.startswith(wanted) or (title or "").lower().startswith(low)
    ]
    return hits[0] if len(hits) == 1 else None


async def _title(ctx: CommandContext) -> Reply:
    from indus.core.persist import StateStore

    p = ctx.pipeline
    sid = await p.session_for(ctx.key, ctx.source)
    wanted = ctx.args.strip()
    if not wanted:
        title = await _title_of(p, sid)
        return f"Title: {title}" if title else "This conversation has no title yet."
    clean = titles.sanitize(wanted)
    if not clean:
        return "That title is empty once cleaned up."
    store = p.service.store
    if isinstance(store, StateStore):
        options = p.service.options
        await store.ensure_session(
            sid,
            source=options.source or options.platform,
            system_prompt="",
            fields=options.session_fields,
        )
    try:
        await run_blocking(titles.set_title, p.store, sid, clean, label="title")
    except titles.TitleTaken:
        return "Another conversation already has that title."
    return f"Title set: {clean}"


def _parse_model(args: str) -> tuple[str, str | None, bool, bool] | str:
    words = args.split()
    flags = {"--global": False, "--once": False}
    provider: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        if word in flags:
            flags[word] = True
        elif word == "--provider":
            if i + 1 >= len(words):
                return "Usage: /model [provider:model] [--provider name] [--global] [--once]"
            provider = words[i + 1]
            i += 1
        else:
            rest.append(word)
        i += 1
    name = " ".join(rest).strip()
    if name and ":" in name and provider is None:
        provider, _, name = name.partition(":")
    if flags["--global"] and flags["--once"]:
        return "Pick one of --global and --once."
    return name, (provider or None), flags["--global"], flags["--once"]


async def _model(ctx: CommandContext) -> Reply:
    from indus.gateway.core.admission import ModelChoice

    p = ctx.pipeline
    parsed = _parse_model(ctx.args)
    if isinstance(parsed, str):
        return parsed
    name, provider, is_global, once = parsed
    if not name:
        current = await p.effective_model(ctx.key, ctx.source)
        where = {"once": "next reply only", "session": "this chat", "channel": "this channel"}
        if current is None:
            return f"Model: {_model_label(p, p.cached_session(ctx.key))} (profile default)"
        return f"Model: {current.label()} ({where.get(current.source, current.source)})"
    choice = ModelChoice(name, provider, None, "once" if once else "session")
    problem = await p.switcher.check(p.service, choice)
    if problem is not None:
        return f"Can't use {choice.label()}: {problem}"
    if once:
        p.once_next[ctx.key] = choice
        return f"The next reply uses {choice.label()}; then this chat goes back to its model."
    sid = await p.session_for(ctx.key, ctx.source)
    if is_global:
        model_section: dict[str, Any] = {"default": name}
        if provider:
            model_section["provider"] = provider
        await run_blocking(
            config_writer.save, p.scope, {"model": model_section}, label="settings write"
        )
        await run_blocking(routes.set_model_override, p.store, ctx.key, None, label="route")
        await _apply_now(p, ctx, sid)
        return f"Saved {choice.label()} as the default model for every conversation."
    await run_blocking(
        routes.set_model_override, p.store, ctx.key, choice.as_override(), label="route"
    )
    await _apply_now(p, ctx, sid)
    return f"This chat now uses {choice.label()}."


async def _apply_now(p: AdmissionPipeline, ctx: CommandContext, sid: str) -> None:
    try:
        await p.apply_model(sid, await p.effective_model(ctx.key, ctx.source, include_once=False))
    except Exception as exc:
        log.warning("could not switch the model now: %s", exc)


async def _sethome(ctx: CommandContext) -> Reply:
    source = ctx.source
    home: dict[str, Any] = {
        "platform": ctx.platform,
        "chat_id": str(source.chat_id),
        "name": source.chat_name or str(source.chat_id),
    }
    if source.thread_id:
        home["thread_id"] = str(source.thread_id)
    try:
        await run_blocking(
            config_writer.save,
            ctx.scope,
            {"platforms": {ctx.platform: {"home_channel": home}}},
            label="settings write",
        )
    except Exception as exc:
        return f"Could not save the home channel: {exc}"
    return f"This chat is now the {ctx.platform} home channel (scheduled results and notices)."


async def _whoami(ctx: CommandContext) -> Reply:
    source = ctx.source
    commands = ctx.pipeline.commands
    policy = commands.policy(source)
    where = "direct message" if scope_of(source.chat_type) == "dm" else "group"
    lines = [
        f"Platform: {ctx.platform}",
        f"Chat: {where}",
        f"You: {source.user_id or '?'}",
        f"Access: {tier_label(policy, source.user_id)}",
    ]
    if policy.enabled and not policy.is_admin(source.user_id):
        names = ", ".join(f"/{c.name}" for c in commands.runnable(source))
        lines.append(f"Commands: {names or 'none'}")
    return "\n".join(lines)


async def _retry(ctx: CommandContext) -> Reply:
    p = ctx.pipeline
    sid = await p.session_id_for(ctx.key)
    if not sid:
        return "There is no message to send again yet."
    taken = await _rewind(p, sid, 1)
    if not taken.removed or not taken.target_text.strip():
        return "There is no message to send again yet."
    return Rewrite(taken.target_text)


async def _do_undo(ctx: CommandContext, count: int) -> str:
    p = ctx.pipeline
    sid = await p.session_id_for(ctx.key)
    if not sid:
        return "There is nothing to undo yet."
    taken = await _rewind(p, sid, count)
    if not taken.removed:
        return "There is nothing to undo yet."
    return f"Took back {taken.removed} stored messages."


async def _undo(ctx: CommandContext) -> Reply:
    raw = ctx.args.strip() or "1"
    if not is_digits(raw) or int(raw) < 1:
        return "Usage: /undo [N]  (how many of your messages to take back, default 1)"
    count = int(raw)
    if ctx.pipeline.settings().destructive_confirm:

        async def run(_choice: str) -> str | None:
            return await _do_undo(ctx, count)

        return ctx.pipeline.commands.confirms.ask(
            ctx,
            question=f"Take back your last {count} message(s) and their replies?",
            run=run,
            switch_key="approvals.destructive_slash_confirm",
        )
    return await _do_undo(ctx, count)


async def _compress(ctx: CommandContext) -> Reply:
    p = ctx.pipeline
    sid = await p.session_id_for(ctx.key)
    if not sid:
        return "Nothing to compress yet."
    if sid not in p.service.attached():
        sid = await p.session_for(ctx.key, ctx.source)
    outcome = await p.service.compress(sid, ctx.args.strip() or None)
    if outcome.reason:
        return outcome.reason
    if outcome.status == "compressed":
        return f"Compressed {outcome.before} messages into {outcome.after}."
    if outcome.status == "nothing":
        return "Nothing to compress yet."
    return f"Compression: {outcome.status}."


async def _approvals(ctx: CommandContext) -> Reply:
    word = ctx.args.strip().lower()
    if not word:
        try:
            mode = _config_view(ctx.scope).get("approvals.mode") or "manual"
        except Exception:
            mode = "manual"
        return f"Approval mode: {mode}. Change it with /approvals manual, smart or off."
    if word not in ("manual", "smart", "off"):
        return "Usage: /approvals [manual|smart|off]"
    await run_blocking(
        config_writer.save, ctx.scope, {"approvals": {"mode": word}}, label="settings write"
    )
    return f"Approval mode saved: {word}."


async def _reload_mcp(ctx: CommandContext) -> Reply:
    p = ctx.pipeline
    mcp = p.service.mcp
    if mcp is None:
        return "This gateway manages no MCP servers."

    async def run(choice: str) -> str | None:
        async def ask(_question: str) -> Any:
            return "always" if choice == "always" else "once"

        outcome = await mcp.reload(ask)
        return str(getattr(outcome, "message", "") or "MCP servers reloaded.")

    if p.settings().mcp_reload_confirm:
        return p.commands.confirms.ask(
            ctx,
            question="Reconnect the MCP servers? Running tools of theirs are stopped.",
            run=run,
            switch_key=None,
        )
    return await run("once")


def _cron_backend(scope: Scope) -> Any:
    from indus.cron import ports as cron_ports
    from indus.service import cron_host

    cron_host.install()  # the process's backend (the daemon has it already; idempotent)
    return cron_ports.backend_for(scope)


def _job_origin(ctx: CommandContext) -> dict[str, Any]:
    """Where a job made from this chat delivers by default: this chat (P24-46)."""
    source = ctx.source
    return {"platform": ctx.platform, "chat_id": str(source.chat_id), "thread_id": source.thread_id}


async def _suggestions(ctx: CommandContext) -> Reply:
    """``/suggestions [list|accept N|dismiss N|refresh]`` (P24-46); accepted jobs deliver here."""
    from indus.cron.suggestions import handle_suggestions

    scope = ctx.scope
    return await run_blocking(
        lambda: handle_suggestions(
            scope,
            ctx.args,
            backend=lambda: _cron_backend(scope),
            surface="gateway",
            origin=_job_origin(ctx),
        ),
        label="suggestions",
    )


async def _blueprint(ctx: CommandContext) -> Reply:
    """``/blueprint [name [slot=value ...]]`` (P24-46): a name alone hands the agent the
    blueprint's questions as the next turn; with every value given, the job is made here."""
    from indus.cron.blueprints import handle_blueprint

    scope = ctx.scope
    reply = await run_blocking(
        lambda: handle_blueprint(
            scope,
            ctx.args,
            backend=lambda: _cron_backend(scope),
            surface="gateway",
            origin=_job_origin(ctx),
        ),
        label="blueprint",
    )
    return Rewrite(reply.seed) if reply.seed else reply.text


async def _not_in_chat(ctx: CommandContext) -> Reply:
    return NOT_IN_CHAT.format(name=ctx.name)


async def _idle_stop(ctx: CommandContext) -> Reply:
    cleared = ctx.pipeline.pending.clear(ctx.key)
    if cleared:
        return f"Nothing was replying; dropped {cleared} queued message(s)."
    return "Nothing is running."


async def _idle_payload(ctx: CommandContext) -> Reply:
    text = ctx.args.strip()
    if not text:
        return f"Usage: /{ctx.name} <text>"
    return Rewrite(text)


# -- mid-run variants ------------------------------------------------------------------------------


async def _busy_stop(ctx: CommandContext) -> Reply:
    p = ctx.pipeline
    cleared = p.pending.clear(ctx.key)
    p.busy_guard.discard(ctx.key)
    sid = p.cached_session(ctx.key)
    stopped = False
    if sid and p.is_running(sid):
        with contextlib.suppress(ProtocolError):
            stopped = p.service.interrupt(sid, hard=True).interrupted
    claim = p.claims.current(ctx.key)
    if claim is not None and not stopped:
        p.drop_claim(ctx.key, cancel=True)  # still setting up: no turn to interrupt yet
        stopped = True
    text = "Stopped." if stopped else "Nothing is running."
    return f"{text} Dropped {cleared} queued message(s)." if cleared else text


async def _busy_new(ctx: CommandContext) -> Reply:
    p = ctx.pipeline
    p.busy_guard.discard(ctx.key)
    # A turn still setting up is cancelled, or it would land in the new conversation.
    p.drop_claim(ctx.key, cancel=not p.is_running(p.cached_session(ctx.key)))
    return await _do_new(ctx)


async def _busy_queue(ctx: CommandContext) -> Reply:
    from indus.gateway.core.admission import admitted_copy

    p = ctx.pipeline
    text = ctx.args.strip()
    event = ctx.event
    if not text and not event.media:
        return "Usage: /queue <text>"
    queued = admitted_copy(event, text=text, allow_control=False)
    if not p.pending.push(ctx.key, queued):
        return "Too many messages are waiting already; this one was dropped."
    p.drain(ctx.key)
    return f"Queued ({p.pending.depth(ctx.key)} waiting)."


async def _busy_steer(ctx: CommandContext) -> Reply:
    p = ctx.pipeline
    text = ctx.args.strip()
    if not text:
        return "Usage: /steer <text>"
    sid = p.cached_session(ctx.key)
    if sid and p.is_running(sid):
        with contextlib.suppress(ProtocolError):
            if p.service.steer(sid, text).queued:
                return "Got it: the reply in progress picks this up after its current step."
    return await _busy_queue(ctx)


_GOAL_CONTROL: Final = frozenset({"", "status", "pause", "resume", "clear", "stop"})
_LOOP_CONTROL: Final = frozenset({"", "list", "status", "stop", "pause", "resume", "clear"})


def _busy_control(name: str, allowed: frozenset[str]) -> Handler:
    forward = _automation(name)

    async def run(ctx: CommandContext) -> Reply:
        word, _ = _first(ctx.args)
        if word not in allowed:
            return f"A new /{name} can't start while I'm replying; control words still work."
        return await forward(ctx)

    return run


# -- tables ----------------------------------------------------------------------------------------

ANYTIME: Final[Mapping[str, Handler]] = {
    "status": _status,
    "approve": _approve,
    "deny": _deny,
    "help": _help,
    "commands": _commands,
    "yolo": _yolo,
    "verbose": _verbose,
    "usage": _usage,
    "bg": _bg,
    "btw": _automation("btw"),
    "platform": _platform,
    "version": _version,
    "subgoal": _automation("subgoal"),
    "heartbeat": _automation("heartbeat"),
}

IDLE_ONLY: Final[Mapping[str, Handler]] = {
    "new": _new,
    "resume": _resume,
    "sessions": _sessions,
    "title": _title,
    "model": _model,
    "sethome": _sethome,
    "whoami": _whoami,
    "retry": _retry,
    "undo": _undo,
    "compress": _compress,
    "approvals": _approvals,
    "personality": _not_in_chat,
    "skills": _not_in_chat,
    "memory": _not_in_chat,
    "reload-mcp": _reload_mcp,
    "moa": _automation("moa"),
    "stop": _idle_stop,
    "queue": _idle_payload,
    "steer": _idle_payload,
    "goal": _automation("goal"),
    "loop": _automation("loop"),
    "suggestions": _suggestions,
    "blueprint": _blueprint,
}

MID_RUN: Final[Mapping[str, Handler]] = {
    "stop": _busy_stop,
    "new": _busy_new,
    "queue": _busy_queue,
    "steer": _busy_steer,
    "goal": _busy_control("goal", _GOAL_CONTROL),
    "loop": _busy_control("loop", _LOOP_CONTROL),
}


class _NotMine:
    pass


_NOT_MINE: Final = _NotMine()


def _default_skills(scope: Scope, platform: str, session_id: str | None) -> Any:
    from indus.learning.skills.bundles import bundle_expanders
    from indus.learning.skills.invoke import CommandTable, skill_command_names
    from indus.learning.skills.resolver import load_catalog

    catalog = load_catalog(scope, platform=platform)
    return CommandTable(
        skills=skill_command_names(catalog), bundles=bundle_expanders(scope, session_id)
    )


class ChatCommands:
    """Command resolution and dispatch for one intake (see the module notes)."""

    def __init__(
        self,
        pipeline: AdmissionPipeline,
        *,
        skills: Callable[[Scope, str, str | None], Any] | None = _default_skills,
        plugin_commands: Callable[[], Mapping[str, PluginCommand]] | None = None,
        command_hooks: CommandHooks | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.skills = skills
        self.plugin_commands = plugin_commands
        self.command_hooks = command_hooks
        self.confirms = PendingConfirms(pipeline)

    # -- access ------------------------------------------------------------------------------------

    def policy(self, source: Source) -> SlashPolicy:
        platform = str(source.platform).lower()
        return policy_for(self.pipeline.extra_for(platform), source.chat_type)

    def denied(self, source: Source, name: str) -> str | None:
        if self.policy(source).can_run(source.user_id, name):
            return None
        return DENIED.format(name=name)

    def runnable(self, source: Source) -> list[CommandDef]:
        policy = self.policy(source)
        return [
            cmd
            for cmd in REGISTRY.commands(SURFACE)
            if (cmd.name in ANYTIME or cmd.name in IDLE_ONLY)
            and policy.can_run(source.user_id, cmd.name)
        ]

    # -- resolution --------------------------------------------------------------------------------

    def resolve(self, word: str) -> CommandDef | tuple[str, ...] | None:
        """A registry entry (name, alias or unique prefix), the candidates of an ambiguous
        prefix, or None."""
        cmd = REGISTRY.resolve(word)
        if cmd is not None:
            return cmd
        match = REGISTRY.prefix_matches(word, REGISTRY.keys(SURFACE))
        if match.kind == "unique":
            return REGISTRY.resolve(match.names[0])
        if match.kind == "ambiguous":
            return match.names
        return None

    # -- dispatch ----------------------------------------------------------------------------------

    async def dispatch_idle(self, event: InboundEvent, key: str, *, hops: int = 0) -> Reply:
        word = event.command or ""
        args = event.command_args
        if word in PLATFORM_PINGS and REGISTRY.resolve(word) is None:
            return None
        if REGISTRY.resolve(word) is None:
            extension = await self._extension(event, key, word, args, busy=False, hops=hops)
            if not isinstance(extension, _NotMine):
                return extension
        found = self.resolve(word)
        if found is None:
            return UNKNOWN.format(name=word)
        if isinstance(found, tuple):
            names = ", ".join(f"/{n}" for n in found[:6])
            return f"/{word} could mean {names}. Which one?"
        cmd = found
        if not cmd.offered_on(SURFACE):
            return TERMINAL_ONLY.format(name=cmd.name)
        denied = self.denied(event.source, cmd.name)
        if denied is not None:
            return denied
        hooked = await self._hooks(event, key, cmd.name, word, args)
        if isinstance(hooked, Rewrite):
            if hops >= MAX_ALIAS_HOPS:
                return "That command rewrites itself in a loop."
            rewritten = event.model_copy(update={"text": hooked.text})
            return await self.dispatch_idle(rewritten, key, hops=hops + 1)
        if not isinstance(hooked, _NotMine):
            return hooked
        handler = ANYTIME.get(cmd.name) or IDLE_ONLY.get(cmd.name) or _not_in_chat
        ctx = CommandContext(self.pipeline, event, key, cmd.name, args)
        return await self._run(handler, ctx)

    async def dispatch_busy(self, event: InboundEvent, key: str) -> str | None:
        """A command while a reply runs: mid-run variant, any-time handler, or the fixed reply."""
        word = event.command or ""
        if word in PLATFORM_PINGS and REGISTRY.resolve(word) is None:
            return None
        found = self.resolve(word)
        if found is None or isinstance(found, tuple):
            return BUSY_REJECT.format(name=word)
        cmd = found
        if not cmd.offered_on(SURFACE):
            return TERMINAL_ONLY.format(name=cmd.name)
        denied = self.denied(event.source, cmd.name)
        if denied is not None:
            return denied
        ctx = CommandContext(self.pipeline, event, key, cmd.name, event.command_args, busy=True)
        handler = MID_RUN.get(cmd.name)
        if handler is None and cmd.busy_policy != "reject":
            handler = ANYTIME.get(cmd.name)
        if handler is None:
            return BUSY_REJECT.format(name=cmd.name)
        answer = await self._run(handler, ctx)
        return answer.text if isinstance(answer, Rewrite) else answer

    async def _run(self, handler: Handler, ctx: CommandContext) -> Reply:
        try:
            return await handler(ctx)
        except ProtocolError as exc:
            return f"/{ctx.name}: {exc.message}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("/%s failed", ctx.name, exc_info=exc)
            return f"/{ctx.name} failed: {type(exc).__name__}"

    async def _hooks(
        self, event: InboundEvent, key: str, name: str, raw: str, args: str
    ) -> Reply | _NotMine:
        if self.command_hooks is None:
            return _NOT_MINE
        context = {
            "platform": str(event.source.platform),
            "user_id": event.source.user_id,
            "command": name,
            "raw_command": raw,
            "args": args,
            "session_key": key,
        }
        try:
            results = await self.command_hooks(f"command:{name}", context)
        except Exception:
            log.warning("command:%s hooks failed (ignored)", name, exc_info=True)
            return _NOT_MINE
        for result in results:
            if not isinstance(result, Mapping):
                continue
            found = cast(Mapping[str, Any], result)
            decision = str(found.get("decision", "")).strip().lower()
            message = found.get("message")
            text = message if isinstance(message, str) and message else None
            if decision == "deny":
                return text or f"/{raw} is blocked here."
            if decision == "handled":
                return text or ""
            if decision == "rewrite":
                target = str(found.get("command_name", "")).strip().lstrip("/")
                if target:
                    rest = str(found.get("raw_args", args)).strip()
                    return Rewrite(f"/{target} {rest}".strip())
        return _NOT_MINE

    async def _extension(
        self, event: InboundEvent, key: str, word: str, args: str, *, busy: bool, hops: int
    ) -> Reply | _NotMine:
        """Quick, plugin and skill commands by exact name."""
        p = self.pipeline
        source = event.source
        quick = quick_commands(p.scope).get(word)
        if quick is not None:
            denied = self.denied(source, word)
            if denied is not None:
                return denied
            kind = str(quick.get("type", "")).lower()
            if kind == "exec":
                command = str(quick.get("command") or "").strip()
                if not command:
                    return f"Quick command /{word} has no command to run."
                return await run_exec_quick(p.scope, command, clock=p.clock)
            if kind == "alias":
                target = str(quick.get("target") or "").strip().lstrip("/")
                if not target or hops >= MAX_ALIAS_HOPS:
                    return f"Quick command /{word} has no usable target."
                rewritten = event.model_copy(update={"text": f"/{target} {args}".strip()})
                return await self.dispatch_idle(rewritten, key, hops=hops + 1)
            return f"Quick command /{word} has an unknown type."
        if self.plugin_commands is not None:
            try:
                plugins = self.plugin_commands()
            except Exception:
                plugins = {}
            plugin = plugins.get(word)
            if plugin is not None:
                denied = self.denied(source, word)
                if denied is not None:
                    return denied
                answer = await plugin(
                    args, {"platform": str(source.platform), "user_id": source.user_id}
                )
                return answer or ""
        return await self._skill(event, key, word, args)

    async def _skill(self, event: InboundEvent, key: str, word: str, args: str) -> Reply | _NotMine:
        if self.skills is None:
            return _NOT_MINE
        p = self.pipeline
        platform = str(event.source.platform).lower()
        sid = p.cached_session(key)
        provider = self.skills
        try:
            table = await run_blocking(provider, p.scope, platform, sid, label="skill commands")
        except Exception:
            log.warning("skill commands could not be listed", exc_info=True)
            return _NOT_MINE
        if table is None or word not in table.names():
            return _NOT_MINE
        denied = self.denied(event.source, word)
        if denied is not None:
            return denied
        line = f"/{word} {args}".strip()
        expansion = await run_blocking(
            lambda: table.run(line, scope=p.scope, session_id=sid), label="skill expansion"
        )
        if expansion is None or not expansion.loaded:
            return f"/{word} could not be loaded."
        return Rewrite(expansion.text)
