"""Slash commands typed in an editor (P19-12).

A prompt made of text only whose first word is a registered command or alias (``/title x``,
``/compact``) is a command; ``/etc/hosts …``, ``/tmp/x`` and unknown ``/words`` are prompt text,
and so is a command sent with attachments. Skill commands (``/<skill> …``) expand into the prompt
the skill writes, as on every surface.

``ROUTES`` is what this adapter answers itself (the rest goes to the backend's slash executor,
``AcpBackend.run_command``):

    help · branch (refused: the editor forks) · steer · queue · title · undo · retry · tools ·
    model · compress · new / clear (start this session over)

Every answer is an ``Outcome``: ``reply`` text shown as an agent message (the prompt then ends
``end_turn``), and/or ``submit`` text that runs as the prompt. A refusal — a backend error, a
mutation the backend cannot do — is the command's reply, never a failed prompt. A command whose
argument is required and missing answers with its usage.

``advertised(scope)`` is the ``available_commands_update`` list: the registry commands offered on
the desktop surface plus ``tools``, without ``branch`` (the editor's fork) and leaving commands,
then the user's alias quick commands and the skill commands.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from indus.acp.backend import BackendError, MutationResult
from indus.foundation.blocking import run_blocking
from indus.service.commands.registry import REGISTRY, CommandDef

if TYPE_CHECKING:
    from indus.acp.agent import AcpAgent, HeldSession
    from indus.foundation.scope import Scope

__all__ = [
    "BRANCH_REFUSED",
    "HIDDEN",
    "ROUTES",
    "Outcome",
    "advertised",
    "command_of",
    "run",
    "skill_expansion",
]

log = logging.getLogger(__name__)

BRANCH_REFUSED: Final = "/branch: use your editor's fork action (it forks this session for you)."
HIDDEN: Final = frozenset({"branch", "quit", "clear", "history", "config"})
"""Registry commands an editor is not offered (``clear`` stays runnable: it resets here)."""
EXTRA: Final = ("tools",)
QUICK_KEY: Final = "quick_commands"
_SURFACE: Final = "desktop"


@dataclass(frozen=True)
class Outcome:
    reply: str | None = None
    submit: str | None = None


type Route = Callable[["AcpAgent", "HeldSession", str], Awaitable[Outcome]]


# -- recognising a command -------------------------------------------------------------------------


def command_of(text: str) -> tuple[str, str] | None:
    """``(canonical name, argument)`` when ``text`` invokes a registered command, else None."""
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return None
    parts = stripped[1:].split(None, 1)
    if not parts:
        return None
    word = parts[0]
    if "/" in word:
        return None
    found = REGISTRY.resolve(word)
    if found is None:
        return None
    return found.name, (parts[1].strip() if len(parts) > 1 else "")


def _offered(cmd: CommandDef) -> bool:
    if cmd.name in HIDDEN:
        return False
    return cmd.offered_on(_SURFACE) or cmd.name in EXTRA


def _quick_aliases(scope: Scope) -> dict[str, str]:
    """The user's alias quick commands: name → target command line."""
    config = scope.config
    try:
        raw = config.get(QUICK_KEY) if config is not None else None
    except Exception:
        return {}
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, str] = {}
    for name, spec in cast(Mapping[object, object], raw).items():
        if not isinstance(name, str) or not isinstance(spec, Mapping):
            continue
        body = cast(Mapping[str, Any], spec)
        target = str(body.get("target") or "").strip()
        if str(body.get("type", "")).lower() == "alias" and target:
            out[name.strip().lstrip("/").lower()] = target
    return out


def _skill_table(scope: Scope, session_id: str | None) -> Any:
    try:
        from indus.learning.skills.bundles import bundle_expanders
        from indus.learning.skills.invoke import CommandTable, skill_command_names
        from indus.learning.skills.resolver import load_catalog

        catalog = load_catalog(scope, platform="acp")
        return CommandTable(
            skills=skill_command_names(catalog), bundles=bundle_expanders(scope, session_id)
        )
    except Exception:
        log.info("skill commands could not be listed", exc_info=True)
        return None


def advertised(scope: Scope | None = None) -> list[dict[str, Any]]:
    """``availableCommands`` for the editor (see the module notes)."""
    out: list[dict[str, Any]] = []
    taken: set[str] = set()
    for cmd in REGISTRY:
        if not _offered(cmd):
            continue
        entry: dict[str, Any] = {"name": cmd.name, "description": cmd.description}
        if cmd.args_hint:
            entry["input"] = {"hint": cmd.args_hint}
        out.append(entry)
        taken.update(cmd.keys)
    if scope is None:
        return out
    for name, target in sorted(_quick_aliases(scope).items()):
        if name not in taken:
            out.append({"name": name, "description": f"runs {target}"})
            taken.add(name)
    table = _skill_table(scope, None)
    if table is not None:
        for name, note in sorted(cast(dict[str, str], table.names()).items()):
            if name not in taken:
                out.append({"name": name, "description": note, "input": {"hint": "instruction"}})
                taken.add(name)
    return out


async def skill_expansion(agent: AcpAgent, held: HeldSession, text: str) -> str | None:
    """The prompt a skill command expands to, or None when ``text`` is no skill command."""
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return None
    word = stripped[1:].split(None, 1)[0].lower() if stripped[1:].strip() else ""
    if not word or "/" in word:
        return None
    scope = agent.scope

    def expand() -> str | None:
        table = _skill_table(scope, held.key)
        if table is None or word not in table.names():
            return None
        expansion = table.run(stripped, scope=scope, session_id=held.key)
        if expansion is None or not expansion.loaded:
            return None
        return str(expansion.text)

    return await run_blocking(expand, label="acp skill command")


def quick_target(scope: Scope, text: str) -> str | None:
    """The command line an alias quick command stands for (``/name rest`` → ``/target rest``)."""
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return None
    parts = stripped[1:].split(None, 1)
    if not parts:
        return None
    target = _quick_aliases(scope).get(parts[0].lower())
    if target is None or REGISTRY.resolve(parts[0]) is not None:
        return None
    rest = parts[1] if len(parts) > 1 else ""
    return f"/{target.lstrip('/')} {rest}".strip()


# -- the routes ------------------------------------------------------------------------------------


async def _mutate(
    agent: AcpAgent, held: HeldSession, operation: str, payload: Mapping[str, Any]
) -> MutationResult:
    backend = await agent.backend()
    return await backend.mutate(held.key, operation, payload)


async def _help(agent: AcpAgent, held: HeldSession, arg: str) -> Outcome:
    wanted = arg.strip().lstrip("/").lower()
    lines = []
    for entry in advertised(agent.scope):
        name = str(entry["name"])
        if wanted and wanted not in name:
            continue
        hint = entry.get("input", {}).get("hint", "")
        usage = f"/{name} {hint}".strip()
        lines.append(f"{usage} — {entry['description']}")
    return Outcome("\n".join(lines) if lines else f"No command matches '{wanted}'.")


async def _branch(agent: AcpAgent, held: HeldSession, arg: str) -> Outcome:
    return Outcome(BRANCH_REFUSED)


async def _steer(agent: AcpAgent, held: HeldSession, arg: str) -> Outcome:
    if not arg:
        return Outcome("Usage: /steer <guidance for the running reply>")
    book = agent.prompts
    state = book.state(held.key)
    backend = await agent.backend()
    if await backend.steer(held.key, arg, admission_id=state.admission_id):
        shown = arg if len(arg) <= 80 else arg[:79] + "…"
        return Outcome(f"Steer queued for the running reply: {shown}")
    cancelled = state.cancelled_text
    if cancelled:
        state.cancelled_text = None
        return Outcome(submit=f"{cancelled}\n\n[Correction: {arg}]")
    return Outcome(submit=arg)


async def _queue(agent: AcpAgent, held: HeldSession, arg: str) -> Outcome:
    if not arg:
        return Outcome("Usage: /queue <prompt to run after the current reply>")
    return Outcome(submit=arg)


async def _title(agent: AcpAgent, held: HeldSession, arg: str) -> Outcome:
    if not arg:
        from indus.acp import store_view

        row = await run_blocking(
            store_view.session_row, agent.scope, held.key, label="acp session row"
        )
        title = row.get("title") if row else None
        return Outcome(f"Title: {title}" if title else "This session has no title yet.")
    done = await _mutate(agent, held, "rename", {"title": arg})
    if done.status == "refused":
        return Outcome(f"/title: {done.data.get('reason') or 'refused'}")
    title = str(done.data.get("title") or arg)
    agent.projection.title(held.key, title)
    if done.status == "deferred":
        return Outcome(f"Title set: {title} (saved after the first reply)")
    return Outcome(f"Session title set: {title}")


async def _undo(agent: AcpAgent, held: HeldSession, arg: str) -> Outcome:
    raw = arg or "1"
    if not raw.isdigit() or int(raw) < 1:
        return Outcome("Usage: /undo [N]  (how many of your messages to take back, default 1)")
    done = await _mutate(agent, held, "rewind", {"count": int(raw)})
    removed = int(done.data.get("removed") or 0)
    if not removed:
        return Outcome("There is nothing to undo yet.")
    text = str(done.data.get("text") or "")
    turns = int(raw)
    reply = f"Took back {turns} turn{'s' if turns != 1 else ''} ({removed} stored messages)."
    if text:
        reply += f"\nRemoved message: {text}"
    return Outcome(reply)


async def _retry(agent: AcpAgent, held: HeldSession, arg: str) -> Outcome:
    done = await _mutate(agent, held, "rewind", {"count": 1})
    text = str(done.data.get("text") or "")
    if not int(done.data.get("removed") or 0) or not text.strip():
        return Outcome("There is no message to send again yet.")
    return Outcome(submit=text)


async def _tools(agent: AcpAgent, held: HeldSession, arg: str) -> Outcome:
    backend = await agent.backend()
    reply = await backend.run_command(held.key, "tools", arg)
    return Outcome(reply.text, reply.submit)


async def _model(agent: AcpAgent, held: HeldSession, arg: str) -> Outcome:
    from indus.acp import model_mode

    if not arg:
        return Outcome(model_mode.current_text(held))
    return Outcome(await model_mode.switch_text(agent, held, arg))


async def _refresh_usage(agent: AcpAgent, held: HeldSession) -> None:
    """The editor's context meter after the conversation shrank (``/compress``, ``/new``)."""
    backend = await agent.backend()
    usage = await backend.usage(held.key)
    if usage is not None:
        agent.projection.send(
            held.key, {"sessionUpdate": "usage_update", "size": usage.size, "used": usage.used}
        )


async def _compress(agent: AcpAgent, held: HeldSession, arg: str) -> Outcome:
    done = await _mutate(agent, held, "compress", {"focus": arg or None})
    await _refresh_usage(agent, held)
    status = str(done.data.get("status") or done.status)
    if status in ("compressed", "applied"):
        return Outcome("Context compressed.")
    if status in ("nothing", "noop"):
        return Outcome("Nothing to compress yet.")
    return Outcome(f"Compression: {status}.")


async def _reset(agent: AcpAgent, held: HeldSession, arg: str) -> Outcome:
    await _mutate(agent, held, "reset", {})
    await _refresh_usage(agent, held)
    return Outcome("Conversation history cleared; this session starts over.")


ROUTES: Final[Mapping[str, Route]] = {
    "help": _help,
    "branch": _branch,
    "steer": _steer,
    "queue": _queue,
    "title": _title,
    "undo": _undo,
    "retry": _retry,
    "tools": _tools,
    "model": _model,
    "compress": _compress,
    "new": _reset,
    "clear": _reset,
}
"""What this adapter answers itself (every other command goes to the backend)."""


async def run(agent: AcpAgent, held: HeldSession, name: str, arg: str) -> Outcome:
    """Run a command (see the module notes); refusals come back as the reply."""
    route = ROUTES.get(name)
    try:
        if route is not None:
            return await route(agent, held, arg)
        backend = await agent.backend()
        reply = await backend.run_command(held.key, name, arg)
        return Outcome(reply.text, reply.submit)
    except BackendError as exc:
        return Outcome(exc.message if exc.message.startswith("/") else f"/{name}: {exc.message}")
