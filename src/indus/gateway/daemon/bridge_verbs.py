"""The control verbs a messaging bridge asks the daemon (P19-16, P19-17).

``indus mcp serve`` reads conversations from the store on its own; sending and answering approval
prompts need the daemon. ``BridgeVerbs(daemon).handlers()`` adds three owner-only verbs to the
control socket (names and wire in ``indus.service.messaging_bridge``):

- ``deliver-message``: one text to one chat through the delivery router (the platform's outbound
  sender, its dead-chat list, formatting, mirror note). The router is given a ledger key of the
  bridge's own (``messaging_bridge.ledger_key``) and a fresh message ref, so the sender records the
  delivery-ledger row before the platform send. A target is either a raw string (parsed with the
  daemon's target grammar, which knows each platform's address forms) or an already resolved
  platform + chat id; broadcasts, home channels and ``origin`` are refused: one named chat only.
- ``approvals-open``: every approval prompt still open in a hosted session, with the routing key
  of the conversation it belongs to (when it is a messaging chat), the command redacted once more,
  its choices and the generation of the turn that asked.
- ``approvals-respond``: the answer through ``DaemonSessionHost.respond`` — the generation fences
  every surface uses (P17-05), so the first answer anywhere wins.

The handlers run on the daemon's loop (they are coroutines) and look the router and the session
host up at call time: either may come up after the control socket.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from indus.cron import targets as cron_targets
from indus.cron.targets import DeliveryTarget
from indus.foundation.blocking import run_blocking
from indus.foundation.redact import redact_text
from indus.gateway.control_socket import Handler, VerbRefused
from indus.protocol.methods import ProtocolError
from indus.protocol.server_requests import ApprovalAnswer, ApprovalChoice, ApprovalRequest
from indus.service import messaging_bridge as wire
from indus.service.fences import Responded, StaleGeneration
from indus.state import gateway_routes

if TYPE_CHECKING:
    from indus.gateway.daemon.process import GatewayDaemon
    from indus.gateway.delivery.router import DeliveryRouter

__all__ = ["BridgeVerbs"]

log = logging.getLogger(__name__)

_LABEL = "a message sent through indus mcp serve"


def _text(params: Mapping[str, Any], key: str) -> str:
    value = params.get(key)
    return value.strip() if isinstance(value, str) else ""


class BridgeVerbs:
    """See the module notes."""

    def __init__(self, daemon: GatewayDaemon) -> None:
        self.daemon = daemon

    def handlers(self) -> dict[str, tuple[Handler, float]]:
        return {
            wire.DELIVER_VERB: (self.deliver, wire.DELIVER_TIMEOUT_S),
            wire.APPROVALS_OPEN_VERB: (self.approvals_open, wire.APPROVAL_TIMEOUT_S),
            wire.APPROVALS_RESPOND_VERB: (self.approvals_respond, wire.APPROVAL_TIMEOUT_S),
        }

    # -- deliver-message --------------------------------------------------------------------------

    def _router(self) -> DeliveryRouter | None:
        from indus.gateway.delivery.parts import RouterPart

        for part in getattr(self.daemon, "parts", ()):
            if isinstance(part, RouterPart):
                return part.router
        return None

    @staticmethod
    def _target(params: Mapping[str, Any]) -> DeliveryTarget:
        raw = _text(params, "target")
        if raw:
            parsed = cron_targets.parse_targets(raw)
            if not parsed.ok:
                raise VerbRefused("bad_target", parsed.problems[0].message)
            found = parsed.targets
            if len(found) != 1 or not found[0].is_explicit:
                raise VerbRefused("bad_target", "the target must name exactly one chat")
            return found[0]
        platform = _text(params, "platform").lower()
        chat_id = _text(params, "chat_id")
        if not platform or not chat_id:
            raise VerbRefused("bad_request", "a target or a platform and chat id are required")
        thread = _text(params, "thread_id") or None
        return DeliveryTarget(platform, chat_id, thread, is_explicit=True)

    async def deliver(self, params: dict[str, Any]) -> Mapping[str, Any]:
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise VerbRefused("bad_request", "the message text is empty")
        if len(text) > wire.MAX_MESSAGE_CHARS:
            raise VerbRefused("bad_request", f"the message is over {wire.MAX_MESSAGE_CHARS} chars")
        target = self._target(params)
        router = self._router()
        if router is None:
            raise VerbRefused("unavailable", "the delivery router is not running")
        where = router.resolve(target)
        if isinstance(where, str):
            return {"sent": False, "error": where}
        receipt = await router.send(
            DeliveryTarget(where.platform, where.chat_id, where.thread_id, is_explicit=True),
            text,
            label=_LABEL,
            session_key=wire.ledger_key(where.platform, where.chat_id),
            message_ref=wire.new_message_ref(),
        )
        answer: dict[str, Any] = {
            "sent": receipt.ok,
            "platform": where.platform,
            "chat_id": where.chat_id,
            "thread_id": where.thread_id,
            "message_id": receipt.message_id,
            "verified": receipt.verified,
        }
        if not receipt.ok:
            answer["error"] = redact_text(receipt.error or "delivery failed")
        return answer

    # -- approvals --------------------------------------------------------------------------------

    async def approvals_open(self, _params: dict[str, Any]) -> Mapping[str, Any]:
        host = getattr(self.daemon, "host", None)
        if host is None:
            # No session host, no prompts to answer: the bridge must not offer the queue.
            raise VerbRefused("unavailable", "the session host is not running")
        rows: list[dict[str, Any]] = []
        for key in host.hosted():
            for entry in host.open_requests(key):
                request = entry.request
                if not isinstance(request, ApprovalRequest):
                    continue
                rows.append(
                    {
                        "id": request.request_id,
                        "session_id": request.session_id,
                        "kind": request.kind,
                        "command": redact_text(request.command),
                        "description": redact_text(request.description),
                        "choices": [choice.value for choice in request.choices],
                        "generation": entry.generation,
                    }
                )
        keys = await self._session_keys({str(row["session_id"]) for row in rows})
        for row in rows:
            row["session_key"] = keys.get(str(row["session_id"]))
        return {"queue": wire.APPROVAL_QUEUE, "approvals": rows}

    async def _session_keys(self, session_ids: set[str]) -> dict[str, str]:
        store = getattr(self.daemon, "store", None)
        if store is None or not session_ids:
            return {}

        def read() -> dict[str, str]:
            found: dict[str, str] = {}
            for session_id in sorted(session_ids):
                routes = gateway_routes.routes_for_session(store, session_id)
                if routes:
                    found[session_id] = routes[0].session_key
            return found

        try:
            return await run_blocking(functools.partial(read), label="approval session keys")
        except Exception:
            log.warning("could not read the routes of open approvals", exc_info=True)
            return {}

    async def approvals_respond(self, params: dict[str, Any]) -> Mapping[str, Any]:
        host = getattr(self.daemon, "host", None)
        if host is None:
            raise VerbRefused("unavailable", "the session host is not running")
        session_id = _text(params, "session_id")
        request_id = _text(params, "request_id")
        generation = params.get("generation")
        if not session_id or not request_id:
            raise VerbRefused("bad_request", "session_id and request_id are required")
        if generation is not None and (not isinstance(generation, int) or generation < 0):
            raise VerbRefused("bad_request", "generation must be a non-negative integer")
        try:
            choice = ApprovalChoice(_text(params, "choice"))
        except ValueError:
            raise VerbRefused("bad_request", "unknown approval choice") from None
        settled = {"resolved": False, "reason": wire.REASON_SETTLED}
        try:
            open_now = {e.request_id: e for e in host.open_requests(session_id)}
        except ProtocolError:
            return settled  # the session is no longer hosted: its prompts went with it
        entry = open_now.get(request_id)
        if entry is None or not isinstance(entry.request, ApprovalRequest):
            return settled
        if choice not in entry.request.choices:
            return {"resolved": False, "reason": wire.REASON_NOT_ALLOWED}
        asked = generation if generation is not None else entry.generation
        result = host.respond(session_id, request_id, asked or 0, ApprovalAnswer(choice=choice))
        if isinstance(result, StaleGeneration):
            return {"resolved": False, "reason": wire.REASON_STALE}
        if isinstance(result, Responded) and result.accepted:
            return {"resolved": True}
        return settled  # answered elsewhere first, or withdrawn meanwhile
