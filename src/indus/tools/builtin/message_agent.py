# pyright: strict
"""``message_agent``: a bot sends a message to another bot's canonical chat (P24-07).

The tool exists only in the ``bots`` toolset, gated to sessions on the ``bots`` platform, and so
only in the ``indus-bots`` bundle: canonical bot chats have it, nothing else does (selecting "all
toolsets" never brings it in).

``target`` names the recipient:

- ``alice`` — the local profile ``alice`` (a local profile name always wins, so another machine's
  name can never hijack it); when no local profile has that name, a registered peer gateway of
  that name (its default profile);
- ``spark/alice`` — profile ``alice`` on peer gateway ``spark``.

A leading ``@`` is ignored. The tool writes the attribution line itself
(``Message from bot @<sender>:``), so the recipient always knows who wrote. The message travels as
a value, never through a shell.

Delivery goes through the session's transport (the handler extra ``bot_transport`` when the turn
offers one, else the one the host installed for the profile with ``install_transport``). Every
failure comes back typed (``reason``, from ``indus.state.bot_chat.FAILURE_REASONS``). A failure a
second attempt can help (runtime offline, delivery timeout, provider rate limit or server error,
context overflow) is retried exactly once; auth, quota and configuration failures, and the rest,
are returned at once. A reply longer than ``MAX_REPLY_CHARS`` arrives as its head with a note.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Final, Protocol

from indus.foundation.redact import redact_text
from indus.foundation.scope import Scope
from indus.state.bot_chat import RETRYABLE_REASONS, FailureReason, classify_failure
from indus.tools.calls import ToolResult
from indus.tools.registry import ToolRegistry, scope_key

__all__ = [
    "MAX_MESSAGE_CHARS",
    "MAX_REPLY_CHARS",
    "SCHEMA",
    "TOOLSET",
    "TOOL_NAME",
    "BotDelivery",
    "BotTarget",
    "BotTransport",
    "DeliveryOutcome",
    "attribution",
    "deliver_with_retry",
    "install_transport",
    "message_agent",
    "parse_target",
    "register_tools",
    "transport_for",
]

TOOL_NAME: Final = "message_agent"
TOOLSET: Final = "bots"
MAX_MESSAGE_CHARS: Final = 64_000
MAX_REPLY_CHARS: Final = 16_000
CALL_DEADLINE_S: Final = 2 * 600.0 + 60.0
_NAME_CHARS: Final = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


class TargetError(ValueError):
    """The target text is not a profile or ``peer/profile`` name."""


@dataclass(frozen=True)
class BotTarget:
    """``name`` alone: a local profile, else a peer of that name; with ``profile``: that
    profile on peer ``name``."""

    name: str
    profile: str | None = None

    @property
    def is_peer_form(self) -> bool:
        return self.profile is not None

    def render(self) -> str:
        return self.name if self.profile is None else f"{self.name}/{self.profile}"


def _part(text: str, label: str) -> str:
    clean = text.strip()
    if not clean or len(clean) > 64 or not set(clean) <= _NAME_CHARS or clean[0] in "-_":
        raise TargetError(f"{label} {text!r} is not a valid name")
    return clean


def parse_target(text: str) -> BotTarget:
    raw = text.strip().removeprefix("@")
    if "/" in raw:
        peer, _, profile = raw.partition("/")
        return BotTarget(_part(peer, "peer").lower(), _part(profile, "profile"))
    return BotTarget(_part(raw, "target").lower())


@dataclass(frozen=True)
class BotDelivery:
    sender: str
    target: BotTarget
    message: str
    """The attributed text, exactly as the recipient sees it."""
    attempt: int = 1


@dataclass(frozen=True)
class DeliveryOutcome:
    ok: bool
    reply: str = ""
    reason: FailureReason | None = None
    error: str = ""
    session_id: str | None = None

    @classmethod
    def delivered(cls, reply: str, session_id: str | None = None) -> DeliveryOutcome:
        return cls(True, reply, None, "", session_id)

    @classmethod
    def failed(cls, reason: FailureReason, error: str) -> DeliveryOutcome:
        return cls(False, "", reason, error)


class BotTransport(Protocol):
    async def deliver(self, delivery: BotDelivery) -> DeliveryOutcome: ...


# -- installed transports -------------------------------------------------------------------------

_lock = threading.Lock()
_installed: dict[str, BotTransport] = {}


def install_transport(scope: Scope, transport: BotTransport) -> Callable[[], None]:
    """Make ``transport`` the profile's delivery path; returns the undo."""
    key = scope_key(scope)
    with _lock:
        _installed[key] = transport

    def undo() -> None:
        with _lock:
            if _installed.get(key) is transport:
                del _installed[key]

    return undo


def transport_for(scope: Scope | None) -> BotTransport | None:
    if scope is None:
        return None
    with _lock:
        return _installed.get(scope_key(scope))


# -- delivery -------------------------------------------------------------------------------------


def attribution(sender: str) -> str:
    return f"Message from bot @{sender}:"


async def _attempt(transport: BotTransport, delivery: BotDelivery) -> DeliveryOutcome:
    try:
        return await transport.deliver(delivery)
    except Exception as exc:  # a transport bug must not end the turn; report it typed
        text = redact_text(f"{type(exc).__name__}: {exc}")[:500]
        return DeliveryOutcome.failed(classify_failure(text), text)


async def deliver_with_retry(
    transport: BotTransport, delivery: BotDelivery
) -> tuple[DeliveryOutcome, int]:
    """Deliver; retry exactly once when the failure is one a second attempt can help.
    Answers the final outcome and the number of attempts made."""
    first = await _attempt(transport, delivery)
    if first.ok or first.reason not in RETRYABLE_REASONS:
        return first, 1
    second = await _attempt(transport, replace(delivery, attempt=2))
    return second, 2


def _cut(reply: str) -> str:
    if len(reply) <= MAX_REPLY_CHARS:
        return reply
    dropped = len(reply) - MAX_REPLY_CHARS
    return reply[:MAX_REPLY_CHARS] + f"\n[… {dropped:,} more characters of the reply were cut]"


async def message_agent(
    args: Mapping[str, Any],
    *,
    scope: Scope | None = None,
    bot_transport: BotTransport | None = None,
) -> str:
    """The ``message_agent`` handler (see the module notes)."""
    raw_target = args.get("target")
    message = args.get("message")
    if not isinstance(raw_target, str) or not raw_target.strip():
        return ToolResult.error("target must name a bot (a profile, or peer/profile)").text
    if not isinstance(message, str) or not message.strip():
        return ToolResult.error("message must be non-empty text").text
    if len(message) > MAX_MESSAGE_CHARS:
        return ToolResult.error(
            f"message has {len(message):,} characters; the limit is {MAX_MESSAGE_CHARS:,}"
        ).text
    try:
        target = parse_target(raw_target)
    except TargetError as exc:
        return ToolResult.error(str(exc)).text
    sender = scope.profile_id if scope is not None else "unknown"
    if not target.is_peer_form and target.name == sender.lower():
        return ToolResult.error("a bot cannot message itself").text
    transport = bot_transport or transport_for(scope)
    if transport is None:
        return ToolResult.error(
            "bot messaging is not available here",
            error_type="runtime_offline",
            reason="runtime_offline",
            target=target.render(),
            retried=False,
        ).text
    delivery = BotDelivery(sender, target, f"{attribution(sender)}\n\n{message.strip()}")
    outcome, attempts = await deliver_with_retry(transport, delivery)
    if outcome.ok:
        body: dict[str, Any] = {
            "status": "delivered",
            "target": target.render(),
            "reply": _cut(outcome.reply),
            "attempts": attempts,
        }
        if not outcome.reply.strip():
            body["note"] = "the recipient chose not to reply"
        return json.dumps(body, ensure_ascii=False)
    reason = outcome.reason or "unknown"
    return ToolResult.error(
        outcome.error or "the message could not be delivered",
        error_type=reason,
        reason=reason,
        target=target.render(),
        retried=attempts > 1,
        retryable=reason in RETRYABLE_REASONS,
    ).text


SCHEMA: Final[dict[str, Any]] = {
    "name": TOOL_NAME,
    "description": (
        "Send a message to another bot and get its reply. The message lands in that bot's own "
        "permanent chat, signed with your name, and the bot answers there. target is the other "
        "bot's profile name (for example researcher), or peer/profile for a bot on another "
        "machine's gateway (for example spark/researcher). Write the message yourself: say who "
        "you are only if it matters, state what you need, and keep it short. A failed delivery "
        "returns a reason code (provider_auth, provider_quota, provider_rate_limit, "
        "provider_server, context_overflow, missing_config, model_unavailable, runtime_offline, "
        "queued_expired, delivery_timeout, target_busy, unknown); it was already retried once "
        "when that could help, so do not resend at once."
    ),
    "parameters": {
        "type": "object",
        "required": ["target", "message"],
        "properties": {
            "target": {
                "description": "The bot to message: profile, or peer/profile.",
                "type": "string",
            },
            "message": {"description": "What to send.", "type": "string"},
        },
    },
}


def register_tools(registry: ToolRegistry) -> None:
    # Two attempts of at most a relayed turn's ten minutes each, plus the calls around them.
    registry.register(TOOL_NAME, TOOLSET, SCHEMA, message_agent, timeout_s=CALL_DEADLINE_S)
