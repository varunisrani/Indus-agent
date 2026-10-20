"""``session/load``, ``session/resume`` and ``session/fork`` (P19-06).

Load and resume do the same thing:

1. the id the editor holds is mapped to the session: compression rotates stored ids, so an old id
   resolves to its conversation and becomes an alias; every update is addressed to the id this
   editor used on its latest load;
2. when the session's folder differs from the editor's (translated, then compared with
   ``paths.same_folder``), a ``workspace`` change moves it (idle sessions only); a session without
   a folder gets a short message instead;
3. a non-empty ``mcpServers`` is the editor's selection for the session; an empty list keeps the
   servers the session has;
4. the session is subscribed and its stored history is streamed as ``session/update``
   notifications **before** the response (R09.9): user chunks, thought chunks, agent chunks, and
   for each tool call a ``tool_call`` start plus a ``tool_call_update`` with its result;
5. open approval/clarify requests are offered again through the subscription; turns lost in a
   daemon restart are announced; the response carries modes, config options and provenance.

Fork branches the parent (the editor's MCP servers for the child, else a copy of the parent's),
moves the child's folder when it differs, subscribes it and answers with the child's id (no
replay: the editor shows a fork as a new thread).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, Final, cast

from indus.acp import paths
from indus.acp.agent import AcpAgent, HeldSession, handler, wire
from indus.acp.backend import AcpBackend, LostTurn, SessionSnapshot
from indus.acp.sessions import attach, editor_policy, notices_after_attach, session_fields
from indus.core.transcript import TranscriptRow, call_arguments, call_id, call_name

__all__ = [
    "MAX_OUTPUT",
    "NO_CWD_TEXT",
    "ToolKinds",
    "announce_lost",
    "lost_text",
    "replay_updates",
]

log = logging.getLogger(__name__)

MAX_OUTPUT: Final = 5000
"""Characters of a replayed tool result shown in the editor."""
NO_CWD_TEXT: Final = (
    "This conversation has no working folder of its own, so the editor's folder is not applied.\n"
)

type ToolKinds = Callable[[str], str]
"""Tool name → ACP tool kind (``read``, ``edit``, ``execute``, …). P19-10 supplies the real map."""


def _other(_name: str) -> str:
    return "other"


_IMAGE_PARTS: Final = frozenset({"image", "image_url", "input_image"})


def _text(content: object) -> str:
    """The text of a stored content value (parts joined; images as ``[image]``), stripped."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        pieces: list[str] = []
        for part in cast(list[object], content):
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, Mapping):
                entry = cast(Mapping[str, Any], part)
                if str(entry.get("type", "")) in _IMAGE_PARTS:
                    pieces.append("[image]")
                elif isinstance(entry.get("text"), str):
                    pieces.append(str(entry["text"]))
        return "\n".join(p.strip() for p in pieces if p.strip()).strip()
    return str(content).strip()


def _chunk(kind: str, text: str) -> dict[str, Any]:
    return {"sessionUpdate": kind, "content": {"type": "text", "text": text}}


def _args(raw: str | None) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {"arguments": raw}


def _cut(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n… ({len(text) - MAX_OUTPUT} more characters not shown)"


def replay_updates(
    rows: Sequence[TranscriptRow], *, kinds: ToolKinds | None = None
) -> Iterator[dict[str, Any]]:
    """The ``session/update`` payloads that rebuild a stored transcript in an editor."""
    kind_of = kinds or _other
    open_calls: dict[str, str] = {}
    for row in rows:
        if row.scaffold or row.display_kind == "hidden":
            continue
        if row.role == "user":
            text = _text(row.content)
            if text:
                yield _chunk("user_message_chunk", text)
        elif row.role == "assistant":
            thought = (row.reasoning or "").strip()
            if thought:
                yield _chunk("agent_thought_chunk", thought)
            text = _text(row.content)
            if text:
                yield _chunk("agent_message_chunk", text)
            for call in row.tool_calls or ():
                ident = call_id(call)
                if not ident:
                    continue
                name = call_name(call) or "tool"
                open_calls[ident] = name
                yield {
                    "sessionUpdate": "tool_call",
                    "toolCallId": ident,
                    "title": name,
                    "kind": kind_of(name),
                    "status": "in_progress",
                    "rawInput": _args(call_arguments(call)),
                }
        elif row.role == "tool":
            ident = (row.tool_call_id or "").strip()
            name = open_calls.pop(ident, None)
            if not ident or name is None:
                continue  # a result whose call is not in the replayed history
            result = _text(row.content)
            failed = bool(row.extra.get("is_error")) if row.extra else False
            update: dict[str, Any] = {
                "sessionUpdate": "tool_call_update",
                "toolCallId": ident,
                "status": "failed" if failed else "completed",
            }
            if result:
                update["content"] = [
                    {"type": "content", "content": {"type": "text", "text": _cut(result)}}
                ]
            yield update


def lost_text(session_id: str, lost: Sequence[LostTurn]) -> str:
    """Why the next prompt in this session will be refused, and how to recover (R09.11)."""
    ids = " and ".join(f"`/discard {turn.admission_id}`" for turn in lost)
    return (
        "A turn in this conversation was lost when the Indus gateway restarted, so its outcome "
        "is unknown. Nothing is sent again from here. Resolve it in a terminal: "
        f"`indus -r {session_id}`, then {ids}, then continue here.\n"
    )


async def announce_lost(agent: AcpAgent, held: HeldSession, lost: Sequence[LostTurn]) -> None:
    if not lost:
        return
    await agent.update(held.editor_id, _chunk("agent_message_chunk", lost_text(held.key, lost)))


async def _replay(agent: AcpAgent, held: HeldSession, snapshot: SessionSnapshot) -> None:
    kinds = getattr(agent, "tool_kinds", None)
    try:
        for update in replay_updates(snapshot.history, kinds=kinds):
            await agent.update(held.editor_id, update)
    except Exception:
        log.warning("the history replay of %s stopped early", held.editor_id, exc_info=True)


async def _move_if_needed(
    agent: AcpAgent, backend: AcpBackend, snapshot: SessionSnapshot, editor_id: str, cwd: str
) -> SessionSnapshot:
    if snapshot.cwd is None:
        await agent.update(editor_id, _chunk("agent_message_chunk", NO_CWD_TEXT))
        return snapshot
    if paths.same_folder(snapshot.cwd, cwd):
        return snapshot
    result = await backend.mutate(snapshot.session_id, "workspace", {"cwd": cwd})
    if result.status != "applied":
        log.info("session %s stays in its folder (%s)", snapshot.session_id, result.status)
        return snapshot
    return SessionSnapshot(**{**snapshot.__dict__, "cwd": cwd})


async def _open(agent: AcpAgent, params: Mapping[str, Any]) -> tuple[HeldSession, SessionSnapshot]:
    editor_id = str(params["sessionId"])
    cwd = paths.translate_cwd(str(params.get("cwd") or ""), wsl=agent.wsl)
    chosen = editor_policy(params.get("mcpServers"))
    backend = await agent.backend()
    known = agent.sessions.key_for(editor_id)
    snapshot = await backend.resume(
        known or editor_id, editor=chosen if chosen.mcp_servers else None
    )
    if cwd:
        snapshot = await _move_if_needed(agent, backend, snapshot, editor_id, cwd)
    held = await attach(agent, backend, snapshot, editor_id)
    await _replay(agent, held, snapshot)
    for pending in snapshot.pending:
        agent.sink.on_request(held.key, pending.request, pending.generation)
    await announce_lost(agent, held, snapshot.lost)
    await backend.show_reasoning(snapshot.session_id)
    return held, snapshot


async def _answer(agent: AcpAgent, held: HeldSession, snapshot: SessionSnapshot) -> dict[str, Any]:
    backend = await agent.backend()
    fields = await session_fields(agent, backend, snapshot, held.editor_id)
    agent.after_reply(lambda: notices_after_attach(agent, held))
    return fields


@handler("session/load")
async def load_session(agent: AcpAgent, params: Mapping[str, Any]) -> dict[str, Any]:
    from acp.schema import LoadSessionRequest, LoadSessionResponse

    LoadSessionRequest.model_validate(params)
    held, snapshot = await _open(agent, params)
    fields = await _answer(agent, held, snapshot)
    response = LoadSessionResponse.model_validate(fields)
    return wire(response)


@handler("session/resume")
async def resume_session(agent: AcpAgent, params: Mapping[str, Any]) -> dict[str, Any]:
    from acp.schema import ResumeSessionRequest, ResumeSessionResponse

    ResumeSessionRequest.model_validate(params)
    held, snapshot = await _open(agent, params)
    fields = await _answer(agent, held, snapshot)
    response = ResumeSessionResponse.model_validate(fields)
    return wire(response)


@handler("session/fork")
async def fork_session(agent: AcpAgent, params: Mapping[str, Any]) -> dict[str, Any]:
    from acp.schema import ForkSessionRequest, ForkSessionResponse

    request = ForkSessionRequest.model_validate(params)
    chosen = editor_policy(params.get("mcpServers"))
    cwd = paths.translate_cwd(request.cwd, wsl=agent.wsl)
    backend = await agent.backend()
    parent = agent.sessions.key_for(request.session_id) or request.session_id
    child = await backend.fork(parent, editor=chosen if chosen.mcp_servers else None)
    snapshot = await backend.resume(child)
    if cwd and snapshot.cwd is not None and not paths.same_folder(snapshot.cwd, cwd):
        result = await backend.mutate(snapshot.session_id, "workspace", {"cwd": cwd})
        if result.status == "applied":
            snapshot = SessionSnapshot(**{**snapshot.__dict__, "cwd": cwd})
    held = await attach(agent, backend, snapshot, snapshot.session_id)
    await backend.show_reasoning(snapshot.session_id)
    fields = await _answer(agent, held, snapshot)
    response = ForkSessionResponse.model_validate({"sessionId": held.editor_id, **fields})
    return wire(response)
