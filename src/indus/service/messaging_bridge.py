"""The daemon verbs a messaging bridge uses over the control socket (P19-16, P19-17).

``indus mcp serve`` reads the store itself but cannot reach a platform or an approval prompt: the
running daemon holds both. It asks the daemon over the owner-only control socket
(``indus.service.gateway_control``) with three verbs, answered by
``indus.gateway.daemon.bridge_verbs``; this module is the half both sides share (it lives below the
surfaces, D-16.246):

- ``deliver-message {target} | {platform, chat_id, thread_id?}, text`` — one text to one chat,
  through the daemon's delivery router and the platform's outbound sender, which records a
  delivery-ledger row (``ledger_key``) before the platform send. Answers ``{sent, platform,
  chat_id, thread_id, message_id, verified, error}``.
- ``approvals-open {}`` — ``{queue: APPROVAL_QUEUE, approvals: [...]}``, every approval prompt
  still open in a session the daemon hosts. The ``queue`` field is the capability: a bridge
  registers its approval tools only when it reads this value back.
- ``approvals-respond {session_id, request_id, generation, choice}`` — the answer through the
  daemon's generation fences; ``{resolved: true}`` or ``{resolved: false, reason}``.

The client functions block (a socket exchange); call them off the event loop. They never raise:
a missing or silent daemon is a ``ControlReply`` with an error code.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from indus.protocol.server_requests import ApprovalChoice
from indus.service import gateway_control
from indus.service.gateway_control import ControlReply

__all__ = [
    "APPROVALS_OPEN_VERB",
    "APPROVALS_RESPOND_VERB",
    "APPROVAL_QUEUE",
    "APPROVAL_TIMEOUT_S",
    "BRIDGE_VERBS",
    "DECISIONS",
    "DELIVER_TIMEOUT_S",
    "DELIVER_VERB",
    "MAX_MESSAGE_CHARS",
    "REASON_NOT_ALLOWED",
    "REASON_SETTLED",
    "REASON_STALE",
    "SEND_KEY_PREFIX",
    "approvals_open",
    "approvals_respond",
    "decision_of",
    "deliver",
    "ledger_key",
    "new_message_ref",
]

DELIVER_VERB: Final = "deliver-message"
APPROVALS_OPEN_VERB: Final = "approvals-open"
APPROVALS_RESPOND_VERB: Final = "approvals-respond"
BRIDGE_VERBS: Final = (DELIVER_VERB, APPROVALS_OPEN_VERB, APPROVALS_RESPOND_VERB)
"""All owner-only: they send as the profile, or answer the profile's approval prompts."""

APPROVAL_QUEUE: Final = "approvals.v1"
"""The approval-queue capability, carried by every ``approvals-open`` answer."""

DELIVER_TIMEOUT_S: Final = 60.0
"""A delivery may wait out a platform's retries; the verb and the client share this budget."""
APPROVAL_TIMEOUT_S: Final = 5.0

MAX_MESSAGE_CHARS: Final = 16_000
"""Longest text a bridge may send in one call (the socket's request limit is 64 KiB)."""

SEND_KEY_PREFIX: Final = "mcp-send"

REASON_SETTLED: Final = "settled"
REASON_STALE: Final = "stale_generation"
REASON_NOT_ALLOWED: Final = "not_allowed"

DECISIONS: Final[Mapping[str, ApprovalChoice]] = {
    "allow-once": ApprovalChoice.ONCE,
    "allow-session": ApprovalChoice.SESSION,
    "allow-always": ApprovalChoice.ALWAYS,
    "deny": ApprovalChoice.DENY,
}
"""The bridge's decision names → our approval choices."""

_NAME_OF: Final = {choice: name for name, choice in DECISIONS.items()}


def decision_of(choice: str) -> str | None:
    """The bridge's name for an approval choice value (``once`` → ``allow-once``)."""
    try:
        return _NAME_OF[ApprovalChoice(choice)]
    except ValueError:
        return None


def ledger_key(platform: str, chat_id: str) -> str:
    """The delivery-ledger session key of a bridge send. Never the chat's conversation key: crash
    recovery treats any newer row under that key as the turn's owed reply."""
    return f"{SEND_KEY_PREFIX}:{platform}:{chat_id}"


def new_message_ref() -> str:
    """One ledger obligation per call (a client's retry is a new message)."""
    return f"{SEND_KEY_PREFIX}:{secrets.token_hex(8)}"


def deliver(
    home: Path,
    text: str,
    *,
    target: str | None = None,
    platform: str | None = None,
    chat_id: str | None = None,
    thread_id: str | None = None,
    timeout: float = DELIVER_TIMEOUT_S,
) -> ControlReply:
    """Ask the daemon serving ``home`` to send ``text``: a raw ``target`` (parsed by the daemon's
    target grammar) or an already resolved ``platform`` + ``chat_id``."""
    params: dict[str, Any] = {"text": text}
    if target is not None:
        params["target"] = target
    else:
        params.update(platform=platform, chat_id=chat_id)
        if thread_id:
            params["thread_id"] = thread_id
    return gateway_control.query(home, DELIVER_VERB, params, timeout=timeout)


def approvals_open(home: Path, *, timeout: float = APPROVAL_TIMEOUT_S) -> ControlReply:
    return gateway_control.query(home, APPROVALS_OPEN_VERB, {}, timeout=timeout)


def approvals_respond(
    home: Path,
    *,
    session_id: str,
    request_id: str,
    generation: int | None,
    choice: ApprovalChoice,
    timeout: float = APPROVAL_TIMEOUT_S,
) -> ControlReply:
    params: dict[str, Any] = {
        "session_id": session_id,
        "request_id": request_id,
        "generation": generation,
        "choice": choice.value,
    }
    return gateway_control.query(home, APPROVALS_RESPOND_VERB, params, timeout=timeout)
