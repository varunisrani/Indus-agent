"""``session/new`` and the parts every session response shares (P19-05).

New session, in order:

1. the editor's cwd is translated (``paths.translate_cwd``: Windows-host forms under WSL);
2. the launch flags this process was started with are frozen into the session's launch policy
   (``creation_policy.launch_flags_from_env``); a safe-mode or ignore-config launch must name an
   explicit model and provider, or creation is refused before anything is created;
3. the backend creates the session (random request id, source ``acp``, editor policy
   ``{mcp_servers, edit_approval_policy: "ask"}`` — the editor's MCP specs stay in memory);
4. live reasoning is asked for (10 s, failures ignored);
5. the answer is ``{sessionId, modes, configOptions, _meta.indus.sessionProvenance}``; after it
   is queued, ``available_commands_update`` and ``usage_update`` follow (R09.9).

The pinned schema (v1.19.0) has no ``models`` field on session responses: the model picker is a
``select`` config option with ``category: "model"`` (its current value ``provider:model``; the
full catalogue arrives with P19-12).
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping, Sequence
from typing import Any, Final

from indus.acp import paths, store_view
from indus.acp.agent import INVALID_PARAMS, AcpAgent, AcpError, HeldSession, handler, wire
from indus.acp.backend import (
    AcpBackend,
    BackendError,
    CreateRequest,
    EditorPolicy,
    LaunchPolicy,
    SessionSnapshot,
)
from indus.foundation.blocking import run_blocking
from indus.service.creation_policy import launch_flags_from_env

__all__ = [
    "ADVERTISED_COMMANDS",
    "DEFAULT_MODE",
    "MODEL_OPTION_ID",
    "attach",
    "available_commands",
    "editor_policy",
    "launch_policy",
    "provenance",
    "session_fields",
]

log = logging.getLogger(__name__)

DEFAULT_MODE: Final = "default"
MODEL_OPTION_ID: Final = "model"
ADVERTISED_COMMANDS: Final[tuple[str, ...]] = (
    "help",
    "model",
    "tools",
    "compress",
    "title",
    "undo",
    "retry",
    "steer",
    "queue",
    "new",
    "clear",
    "usage",
)
"""Registry commands an editor is offered until P19-12 owns the list."""

BYPASS_REFUSED: Final = (
    "Safe mode and ignore-user-config read no profile defaults, so this launch must name both a "
    "model and a provider, e.g. `INDUS_MODEL=<model> INDUS_PROVIDER=<provider> indus acp` with "
    "the same switches."
)


# -- inputs ---------------------------------------------------------------------------------------


def editor_policy(raw: object) -> EditorPolicy:
    """The editor's ``mcpServers`` (validated against the vendor models, kept as sent)."""
    if raw is None:
        return EditorPolicy()
    if not isinstance(raw, list):
        raise AcpError(INVALID_PARAMS, "mcpServers must be a list", {"path": "mcpServers"})
    from acp.schema import HttpMcpServer, McpServerStdio, SseMcpServer
    from pydantic import TypeAdapter

    adapter: TypeAdapter[Any] = TypeAdapter(HttpMcpServer | SseMcpServer | McpServerStdio)
    specs: list[Mapping[str, Any]] = []
    for item in raw:  # pyright: ignore[reportUnknownVariableType]
        model = adapter.validate_python(item)
        specs.append(wire(model))
    return EditorPolicy(mcp_servers=tuple(specs))


def _first(env: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    return None


def launch_policy(env: Mapping[str, str]) -> LaunchPolicy:
    """The launch choices of this process (switches from the env, plus an explicit route)."""
    fields: dict[str, Any] = dict(launch_flags_from_env(env))
    model = _first(env, "INDUS_MODEL", "INDUS_INFERENCE_MODEL")
    provider = _first(env, "INDUS_PROVIDER", "INDUS_INFERENCE_PROVIDER")
    policy = LaunchPolicy(fields)
    if policy.bypass:
        if not model or not provider:
            raise BackendError("creation_refused", BYPASS_REFUSED)
        fields["model"] = model
        fields["provider"] = provider
    return LaunchPolicy(fields)


# -- shared response parts ------------------------------------------------------------------------


def modes() -> dict[str, Any]:
    """The one mode both hosts offer (D-16.29): ask before edits."""
    return {
        "currentModeId": DEFAULT_MODE,
        "availableModes": [
            {"id": DEFAULT_MODE, "name": "Default", "description": "Ask before edits."}
        ],
    }


def model_option(model: str | None, provider: str | None) -> dict[str, Any] | None:
    if not model:
        return None
    value = f"{provider}:{model}" if provider else model
    return {
        "type": "select",
        "id": MODEL_OPTION_ID,
        "name": "Model",
        "category": "model",
        "currentValue": value,
        "options": [{"value": value, "name": model}],
    }


def provenance(scope: Any, editor_id: str, session_id: str) -> dict[str, Any] | None:
    """``_meta`` with ``indus.sessionProvenance`` (blocking; None when the store has no row)."""
    found = store_view.lineage_of(scope, session_id)
    if found is None:
        return None
    return {
        "indus": {
            "sessionProvenance": {
                "acpSessionId": editor_id,
                "currentSessionId": found.current,
                "rootSessionId": found.root,
                "parentSessionId": found.parent,
                "sessionKind": "continuation" if found.continuation else "root",
                "compressionDepth": found.depth,
            }
        }
    }


async def session_fields(
    agent: AcpAgent, backend: AcpBackend, snapshot: SessionSnapshot, editor_id: str
) -> dict[str, Any]:
    """``modes``, ``configOptions`` and ``_meta`` of a new/load/resume/fork answer."""
    fields: dict[str, Any] = {"modes": modes()}
    option = model_option(snapshot.model, snapshot.provider)
    if option is not None:
        fields["configOptions"] = [option]
    current = backend.current_id(snapshot.session_id)
    meta = await run_blocking(
        lambda: provenance(agent.scope, editor_id, current), label="acp provenance"
    )
    if meta is not None:
        fields["_meta"] = meta
    return fields


def available_commands() -> list[dict[str, Any]]:
    """``availableCommands`` from the shared slash registry (the editor subset)."""
    from indus.service.commands.registry import REGISTRY

    out: list[dict[str, Any]] = []
    for name in ADVERTISED_COMMANDS:
        found = REGISTRY.resolve(name)
        if found is None or found.name != name:
            continue
        entry: dict[str, Any] = {"name": name, "description": found.description}
        if found.args_hint:
            entry["input"] = {"hint": found.args_hint}
        out.append(entry)
    return out


async def notices_after_attach(agent: AcpAgent, held: HeldSession) -> None:
    """``available_commands_update`` then ``usage_update`` (when the window is known)."""
    try:
        await agent.update(
            held.editor_id,
            {
                "sessionUpdate": "available_commands_update",
                "availableCommands": available_commands(),
            },
        )
        backend = await agent.backend()
        usage = await backend.usage(held.key)
        if usage is not None:
            await agent.update(
                held.editor_id,
                {"sessionUpdate": "usage_update", "size": usage.size, "used": usage.used},
            )
    except Exception:
        log.debug("post-attach notices for %s not delivered", held.editor_id, exc_info=True)


async def attach(
    agent: AcpAgent, backend: AcpBackend, snapshot: SessionSnapshot, editor_id: str
) -> HeldSession:
    """Hold the session for this editor and subscribe it (once)."""
    held = agent.sessions.hold(snapshot.session_id, editor_id)
    held.cwd = snapshot.cwd
    held.title = snapshot.title
    if held.unsubscribe is None:
        held.unsubscribe = backend.subscribe(snapshot.session_id, agent.sink)
    return held


# -- the handler ----------------------------------------------------------------------------------


@handler("session/new")
async def new_session(agent: AcpAgent, params: Mapping[str, Any]) -> dict[str, Any]:
    from acp.schema import NewSessionRequest, NewSessionResponse

    request = NewSessionRequest.model_validate(params)
    editor = editor_policy(params.get("mcpServers"))
    cwd = paths.translate_cwd(request.cwd, wsl=agent.wsl)
    launch = launch_policy(agent.env)
    backend = await agent.backend()
    snapshot = await backend.create(
        CreateRequest(cwd=cwd, request_id=secrets.token_hex(16), editor=editor, launch=launch)
    )
    held = await attach(agent, backend, snapshot, snapshot.session_id)
    await backend.show_reasoning(snapshot.session_id)
    fields = await session_fields(agent, backend, snapshot, held.editor_id)
    response = NewSessionResponse.model_validate({"sessionId": held.editor_id, **fields})
    agent.after_reply(lambda: notices_after_attach(agent, held))
    return wire(response)


def server_names(specs: Sequence[Mapping[str, Any]]) -> list[str]:
    return [str(spec.get("name", "")) for spec in specs]
