"""The model picker and the single mode (P19-12, D-16.29, D-16.601).

Model: the pinned schema (v1.19.0) has no ``session/set_model``; the picker is the ``configOptions``
select ``model`` (values ``provider:model``, ``custom:<name>:<model>`` for a named endpoint, or a
bare model on the session's provider), changed with ``session/set_config_option`` and by
``/model <id>``. The backend's ``model`` change answers:

- ``applied`` → the session uses the target from now on (the profile default is never touched);
- ``deferred`` → a turn is running: the pick applies at the next turn ("… applies from your next
  message"); a deferred pick refused at that boundary arrives later as a notice;
- ``confirm`` → a guarded target (the conversation does not fit its window): a permission card asks
  "Switch anyway" (``allow_once``) / "Cancel" (``reject_once``); yes repeats the change with
  ``confirm``; no or a dismissed card → ``model_switch_cancelled`` and nothing changes;
- ``refused`` → the reason (an unknown provider or model, a backend that cannot switch).

Mode: one mode, ``default`` (ask before edits), in both hosts. ``session/set_mode`` to anything
else, or ``session/set_config_option`` of the edit approval policy to anything but ``ask`` →
``acp_edit_policy_mutation_unavailable``; any other config write is accepted and changes nothing.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final, cast

from indus.acp.agent import INTERNAL_ERROR, INVALID_PARAMS, RESOURCE_NOT_FOUND, AcpError, handler
from indus.acp.sessions import DEFAULT_MODE, MODEL_OPTION_ID, model_option

if TYPE_CHECKING:
    from indus.acp.agent import AcpAgent, HeldSession

__all__ = [
    "EDIT_POLICY_ID",
    "POLICY_REFUSED",
    "SWITCH_CANCELLED",
    "current_text",
    "parse_model_id",
    "switch",
    "switch_text",
]

EDIT_POLICY_ID: Final = "edit_approval_policy"
POLICY_REFUSED: Final = "acp_edit_policy_mutation_unavailable"
SWITCH_CANCELLED: Final = "model_switch_cancelled"


class _Cancelled(Exception):
    pass


def parse_model_id(value: str) -> tuple[str | None, str]:
    """``provider:model`` → (provider, model); ``custom:<name>:<model>`` → (``custom:<name>``,
    model); a bare model → (None, model)."""
    text = value.strip()
    if text.startswith("custom:"):
        _, name, rest = (text.split(":", 2) + ["", ""])[:3]
        if name and rest:
            return f"custom:{name}", rest
    head, sep, tail = text.partition(":")
    if sep and head and tail and "/" not in head:
        return head, tail
    return None, text


def current_text(held: HeldSession) -> str:
    if not held.model:
        return "Current model: unknown"
    lines = [f"Current model: {held.model}"]
    if held.provider:
        lines.append(f"Provider: {held.provider}")
    return "\n".join(lines)


def _held(agent: AcpAgent, editor_id: str) -> HeldSession:
    held = agent.sessions.by_editor(editor_id)
    if held is None:
        raise AcpError(RESOURCE_NOT_FOUND, "no such session", {"reason": "not_found"})
    return held


async def _confirm(agent: AcpAgent, held: HeldSession, target: str, message: str) -> bool:
    """The guard card: True only for "Switch anyway"."""
    if agent.peer is None:
        return False
    card_id = f"model-confirm-{secrets.token_hex(6)}"
    params = {
        "sessionId": held.editor_id,
        "toolCall": {
            "toolCallId": card_id,
            "title": f"Switch model: {target}",
            "kind": "other",
            "status": "pending",
            "content": [{"type": "content", "content": {"type": "text", "text": message}}],
            "rawInput": {"model": target},
        },
        "options": [
            {"optionId": "allow_once", "name": "Switch anyway", "kind": "allow_once"},
            {"optionId": "cancel", "name": "Cancel", "kind": "reject_once"},
        ],
    }
    try:
        response = await agent.peer.request("session/request_permission", params)
    except Exception:
        response = None
    body = cast(Mapping[str, Any], response) if isinstance(response, Mapping) else {}
    outcome = body.get("outcome")
    picked = cast(Mapping[str, Any], outcome) if isinstance(outcome, Mapping) else {}
    allowed = picked.get("outcome") == "selected" and picked.get("optionId") == "allow_once"
    agent.projection.send(
        held.key,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": card_id,
            "status": "completed" if allowed else "failed",
        },
    )
    return allowed


async def switch(agent: AcpAgent, held: HeldSession, value: str) -> tuple[str, Mapping[str, Any]]:
    """Change the session's model (see the module notes): ``(status, data)``; raises
    ``_Cancelled`` when the guard card was declined."""
    provider, model = parse_model_id(value)
    if not model:
        return "refused", {"reason": "no model named"}
    payload: dict[str, Any] = {"model": model, "provider": provider or held.provider}
    backend = await agent.backend()
    done = await backend.mutate(held.key, "model", payload)
    if done.status == "confirm":
        message = str(done.data.get("message") or "This model needs confirming.")
        if not await _confirm(agent, held, value, message):
            raise _Cancelled
        done = await backend.mutate(held.key, "model", {**payload, "confirm": True})
    if done.status == "applied":
        held.model = str(done.data.get("model") or model)
        found = done.data.get("provider") or payload["provider"]
        held.provider = str(found) if found else held.provider
    return done.status, done.data


def _deferred_text(data: Mapping[str, Any], fallback: str) -> str:
    return f"{data.get('model') or fallback} applies from your next message."


async def switch_text(agent: AcpAgent, held: HeldSession, value: str) -> str:
    """``/model <id>``: the switch, answered as text (a declined guard included)."""
    try:
        status, data = await switch(agent, held, value)
    except _Cancelled:
        return "Model switch cancelled; the session keeps its current model."
    if status == "applied":
        return current_text(held).replace("Current model", "Model switched to", 1)
    if status == "deferred":
        return _deferred_text(data, value)
    return f"Model not switched: {data.get('reason') or status}."


def _options(held: HeldSession) -> list[dict[str, Any]]:
    option = model_option(held.model, held.provider)
    return [option] if option is not None else []


@handler("session/set_mode")
async def set_mode(agent: AcpAgent, params: Mapping[str, Any]) -> dict[str, Any]:
    from acp.schema import SetSessionModeRequest

    request = SetSessionModeRequest.model_validate(params)
    _held(agent, request.session_id)
    if request.mode_id != DEFAULT_MODE:
        raise AcpError(
            INVALID_PARAMS,
            f"mode {request.mode_id!r} is not available: Indus asks before edits in editor "
            "sessions",
            {"reason": POLICY_REFUSED},
        )
    return {}


@handler("session/set_config_option")
async def set_config_option(agent: AcpAgent, params: Mapping[str, Any]) -> dict[str, Any]:
    from acp.schema import SetSessionConfigOptionResponse

    editor_id = str(params.get("sessionId") or "")
    config_id = str(params.get("configId") or "")
    value = params.get("value")
    held = _held(agent, editor_id)
    if config_id == EDIT_POLICY_ID and value != "ask":
        raise AcpError(
            INVALID_PARAMS,
            "only the ask-before-edits policy is available in editor sessions",
            {"reason": POLICY_REFUSED},
        )
    if config_id == MODEL_OPTION_ID and isinstance(value, str) and value.strip():
        try:
            status, data = await switch(agent, held, value)
        except _Cancelled:
            raise AcpError(
                INTERNAL_ERROR, "the model switch was cancelled", {"reason": SWITCH_CANCELLED}
            ) from None
        if status == "refused":
            raise AcpError(
                INTERNAL_ERROR,
                f"the model was not switched: {data.get('reason') or 'refused'}",
                {"reason": "model_switch_refused"},
            )
        if status == "deferred":
            agent.projection.say(held.key, _deferred_text(data, value) + "\n")
            await agent.projection.flush()
    response = SetSessionConfigOptionResponse.model_validate({"configOptions": _options(held)})
    return response.model_dump(mode="json", by_alias=True, exclude_none=True)
