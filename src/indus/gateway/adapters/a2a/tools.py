# pyright: strict
"""``a2a_call``: send a message to a configured A2A peer agent and return its answer (P24-10).

Peers are configured per profile under ``a2a.peers.<name>``: ``url`` (the peer's JSON-RPC
endpoint), ``trusted`` (default false) and ``timeout_seconds`` (default 120). A peer's key, when it
needs one, lives in the secret store as ``INDUS_A2A_PEER_<NAME>_KEY`` and goes out as a bearer
token; it never appears in the tool's arguments, result or errors.

Before anything leaves: the message is masked for credential shapes and the profile's own secret
values, and a privacy note is put in front of it (the receiving agent is told the content comes
from an agent acting for its user and is confidential). The URL passes the shared SSRF policy
(``indus.safety.url_policy``) and the connect-time transport that re-checks it, so link-local,
loopback and private addresses are refused unless the peer is marked ``trusted: true``. Redirects
are not followed.

The result names the peer, the context id (pass it back as ``context_id`` to continue the
conversation) and the task state; an ``input-required`` answer says so and how to reply.
The tool is offered only when the profile has at least one peer configured.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final, cast

import httpx

from indus.foundation.redact import redact_text
from indus.foundation.scope import Scope
from indus.gateway.adapters.a2a import protocol as p
from indus.safety.safe_transport import ConnectBlocked, safe_client
from indus.safety.url_policy import Blocked, check_url_async
from indus.tools.calls import ToolResult
from indus.tools.registry import ToolRegistry, ToolTraits

__all__ = [
    "A2A_CALL_SCHEMA",
    "PRIVACY_NOTE",
    "TOOL_NAME",
    "TOOLSET",
    "a2a_call",
    "call_peer",
    "peer_key_env",
    "register_tools",
]

log = logging.getLogger(__name__)

TOOL_NAME: Final = "a2a_call"
TOOLSET: Final = "a2a"
DEFAULT_TIMEOUT: Final = 120.0
MAX_REPLY: Final = 64 * 1024
PRIVACY_NOTE: Final = (
    "[Sent by an Indus agent on behalf of its user. The content below may include that user's "
    "private information: use it only to answer this request, keep it confidential and do not "
    "pass it on.]\n\n"
)

type Resolver = Callable[[str, int | None], Sequence[str]]


def peer_key_env(name: str) -> str:
    return "INDUS_A2A_PEER_" + re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_KEY"


def _peers(scope: Scope) -> Mapping[str, Any]:
    if scope.config is None:
        return {}
    try:
        found = scope.config.get("a2a.peers")
    except Exception:
        return {}
    return cast(Mapping[str, Any], found) if isinstance(found, Mapping) else {}


def _known_secrets(scope: Scope) -> list[str]:
    if scope.secrets is None:
        return []
    try:
        return list(scope.secrets.known_values())
    except Exception:
        return []


def _fixed(
    transport: httpx.AsyncBaseTransport,
) -> Callable[[str | None], httpx.AsyncBaseTransport]:
    def factory(proxy: str | None) -> httpx.AsyncBaseTransport:
        return transport

    return factory


def _error(text: str) -> str:
    return ToolResult.error(text).text


def _reply_of(result: object) -> tuple[str, str, str]:
    """``(text, state, context)`` from a ``SendMessage`` result (task or message, 1.0 or 0.3)."""
    if not isinstance(result, Mapping):
        return "", "", ""
    body = cast(Mapping[str, Any], result)
    inner = body.get("task") or body.get("message") or body
    if not isinstance(inner, Mapping):
        return "", "", ""
    item = cast(Mapping[str, Any], inner)
    context = item.get("contextId")
    status = item.get("status")
    state = ""
    text = ""
    artifacts = item.get("artifacts")
    for artifact in cast(list[object], artifacts) if isinstance(artifacts, list) else []:
        text = p.message_text(artifact)
        if text:
            break
    if isinstance(status, Mapping):
        found = cast(Mapping[str, Any], status)
        state = str(found.get("state") or "")
        if not text:
            text = p.message_text(found.get("message"))
    if not text and "parts" in item:
        text = p.message_text(item)
    return text, state, context if isinstance(context, str) else ""


async def call_peer(
    scope: Scope,
    name: str,
    message: str,
    *,
    context_id: str | None = None,
    inner_transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None,
) -> str:
    """Send ``message`` to the configured peer ``name``; the text the model sees."""
    raw = _peers(scope).get(name)
    if not isinstance(raw, Mapping):
        known = ", ".join(sorted(_peers(scope))) or "none"
        return _error(f"unknown A2A peer {name!r} (configured: {known}); add it under a2a.peers")
    peer = cast(Mapping[str, Any], raw)
    url = peer.get("url")
    if not isinstance(url, str) or not url.strip():
        return _error(f"A2A peer {name!r} has no url")
    url = url.strip()
    if context_id is not None and not p.CONTEXT_ID.match(context_id):
        return _error("context_id must be 1-128 characters of [A-Za-z0-9_.:-]")
    trusted = peer.get("trusted") is True
    timeout = peer.get("timeout_seconds")
    seconds = (
        float(timeout)
        if isinstance(timeout, int | float) and not isinstance(timeout, bool) and timeout > 0
        else DEFAULT_TIMEOUT
    )
    from indus.gateway.config import read_secret

    key = read_secret(scope, peer_key_env(name))
    secrets = [*_known_secrets(scope), *([key] if key else [])]
    text = PRIVACY_NOTE + redact_text(message.strip(), secrets)
    request_id = uuid.uuid4().hex
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "SendMessage",
        "params": {"message": p.make_message(p.ROLE_USER, text, context_id=context_id)},
    }
    headers = {"content-type": "application/json", "a2a-version": p.PROTOCOL_VERSION}
    if key:
        headers["authorization"] = f"Bearer {key}"

    verdict = await check_url_async(url, scope, resolver=resolver)
    # Trust opens private, loopback and link-local addresses (and names our resolver cannot
    # see); never a bad scheme, a credential in the URL, the blocklist or cloud metadata.
    if isinstance(verdict, Blocked) and not (trusted and verdict.kind in ("private", "dns")):
        hint = " (mark the peer trusted: true to allow a private address)" if not trusted else ""
        return _error(f"A2A peer {name!r} refused by the URL policy: {verdict.reason}{hint}")
    if trusted:
        client = httpx.AsyncClient(
            transport=inner_transport, trust_env=False, follow_redirects=False, timeout=seconds
        )
    else:
        factory = _fixed(inner_transport) if inner_transport is not None else None
        client = safe_client(
            scope, resolver=resolver, inner_factory=factory, follow_redirects=False, timeout=seconds
        )
    try:
        async with client:
            response = await client.post(url, json=body, headers=headers)
    except ConnectBlocked as exc:
        return _error(f"A2A peer {name!r} refused by the URL policy: {exc.verdict.reason}")
    except httpx.TimeoutException:
        return _error(f"A2A peer {name!r} did not answer within {seconds:g} seconds")
    except httpx.HTTPError as exc:
        return _error(f"could not reach A2A peer {name!r}: {type(exc).__name__}")
    if response.status_code in (401, 403):
        return _error(f"A2A peer {name!r} refused our credentials (HTTP {response.status_code})")
    if response.status_code == 429:
        return _error(f"A2A peer {name!r} is rate limiting us (HTTP 429); try later")
    if response.status_code >= 400:
        return _error(f"A2A peer {name!r} answered HTTP {response.status_code}")
    try:
        answer: object = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return _error(f"A2A peer {name!r} did not answer JSON")
    if not isinstance(answer, Mapping):
        return _error(f"A2A peer {name!r} sent an answer that is not a JSON-RPC response")
    reply = cast(Mapping[str, Any], answer)
    problem = reply.get("error")
    if isinstance(problem, Mapping):
        detail = str(cast(Mapping[str, Any], problem).get("message") or "error")[:300]
        return _error(f"A2A peer {name!r} returned an error: {redact_text(detail, secrets)}")
    reply_text, state, context = _reply_of(reply.get("result"))
    if len(reply_text) > MAX_REPLY:
        reply_text = reply_text[:MAX_REPLY] + "\n[… the peer's answer was cut here]"
    short = state.removeprefix("TASK_STATE_").replace("_", "-").lower()
    head = (
        f"[{name} · context {context or context_id or '?'}" + (f" · {short}" if short else "") + "]"
    )
    out = f"{head}\n{reply_text or '(no text in the answer)'}"
    if state == p.INPUT_REQUIRED:
        out += (
            f"\n\n(The peer needs more input: call {TOOL_NAME} again with context_id "
            f"{context or context_id!r} and your answer.)"
        )
    return out


async def a2a_call(args: Mapping[str, Any], *, scope: Scope | None = None) -> str:
    """The ``a2a_call`` handler."""
    if scope is None:
        return _error("a2a_call needs a profile (no scope here)")
    agent = str(args.get("agent") or "").strip()
    message = str(args.get("message") or "").strip()
    if not agent or not message:
        return _error("both agent and message are required")
    context = args.get("context_id")
    return await call_peer(
        scope, agent, message, context_id=str(context).strip() if context else None
    )


def _available(scope: Scope) -> bool:
    return bool(_peers(scope))


A2A_CALL_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Send a message to another AI agent (an A2A peer configured by the user) and get its "
        "answer. The peer is a separate agent, not a subagent you control; what you send leaves "
        "this machine, so send only what the request needs. Pass context_id from an earlier "
        "answer to continue that conversation; an answer marked input-required is a question "
        "back to you."
    ),
    "parameters": {
        "type": "object",
        "required": ["agent", "message"],
        "properties": {
            "agent": {"type": "string", "description": "The peer's configured name."},
            "message": {"type": "string", "description": "What to ask or tell the peer."},
            "context_id": {
                "type": "string",
                "description": "The context id of an earlier answer, to continue it.",
            },
        },
    },
}


def register_tools(registry: ToolRegistry) -> None:
    registry.register(
        TOOL_NAME,
        TOOLSET,
        A2A_CALL_SCHEMA,
        a2a_call,
        probe=_available,
        traits=ToolTraits(mutating=True),
    )
