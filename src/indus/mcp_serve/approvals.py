"""The approval tools of ``indus mcp serve`` (P19-17, D-16.30), wired to the daemon's queue.

The two tools exist only when ``Approvals.probe()`` found, at server start, a daemon whose
``approvals-open`` answer carries the approval-queue capability; otherwise they are not registered
at all (the reference advertised them hollow, R09.10).

- ``list_open()`` reads the daemon's open approval prompts (every hosted session): id, the
  conversation's session key when it is a chat, the redacted command, why, the decisions the prompt
  allows and the generation of the turn that asked.
- ``respond(id, decision)`` reads the queue again (never a stale copy), then answers through the
  daemon's generation-bound control: the first answer anywhere wins; an id this server saw that is
  gone, or a second answer, is ``{resolved: false, reason: settled}``.
- ``watch()`` runs after event-bridge ticks, asking the daemon at most every ``WATCH_SECONDS``:
  a prompt seen for the first time becomes an ``approval_requested`` event, a prompt that is gone
  an ``approval_resolved`` event (with the decision when this server gave it).

Daemon calls block (a socket exchange) and run on the blocking pool.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final, cast

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.mcp_serve.event_bridge import EventBridge
from indus.mcp_serve.send import daemon_error
from indus.service import messaging_bridge as wire
from indus.service.gateway_control import ControlReply

__all__ = ["DECISION_NAMES", "WATCH_SECONDS", "Approvals"]

log = logging.getLogger(__name__)

WATCH_SECONDS: Final = 1.0
DECISION_NAMES: Final = tuple(wire.DECISIONS)
_SEEN_CAP: Final = 4096


async def _on_pool[T](fn: Callable[[], T]) -> T:
    return await run_blocking(fn, label="mcp serve daemon call")


def _rows(reply: ControlReply) -> list[dict[str, Any]] | None:
    result = reply.result
    if not reply.ok or result is None or result.get("queue") != wire.APPROVAL_QUEUE:
        return None
    raw = result.get("approvals")
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in cast(list[object], raw):
        if isinstance(item, Mapping) and isinstance(cast(Mapping[str, Any], item).get("id"), str):
            rows.append(dict(cast(Mapping[str, Any], item)))
    return rows


def _public(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("choices")
    choices = [str(c) for c in cast(list[object], raw)] if isinstance(raw, list) else []
    names = [name for name in (wire.decision_of(c) for c in choices) if name is not None]
    return {
        "id": row.get("id"),
        "session_key": row.get("session_key"),
        "session_id": row.get("session_id"),
        "kind": row.get("kind", "approval"),
        "command": row.get("command", ""),
        "description": row.get("description", ""),
        "choices": names,
        "generation": row.get("generation"),
    }


class Approvals:
    """See the module notes."""

    def __init__(
        self,
        scope: Scope,
        *,
        bridge: EventBridge | None = None,
        clock: Clock | None = None,
        open_queue: Callable[..., ControlReply] = wire.approvals_open,
        answer: Callable[..., ControlReply] = wire.approvals_respond,
        offload: Callable[[Callable[[], Any]], Awaitable[Any]] | None = None,
    ) -> None:
        self.scope = scope
        self.bridge = bridge
        self.clock: Clock = clock or scope.clock
        self._open = open_queue
        self._answer = answer
        self._offload = offload or _on_pool
        self.available = False
        self._known: dict[str, dict[str, Any]] = {}
        self._seen: dict[str, None] = {}
        self._answered: dict[str, str] = {}
        self._next_watch: float | None = None

    async def _fetch(self) -> ControlReply:
        home = self.scope.home
        return await self._offload(lambda: self._open(home))

    async def probe(self) -> bool:
        """Whether the daemon offers the approval queue (checked once, at server start)."""
        try:
            reply = await self._fetch()
        except Exception:
            log.debug("approval queue probe failed", exc_info=True)
            return False
        self.available = _rows(reply) is not None
        return self.available

    def _remember(self, approval_id: str) -> None:
        self._seen[approval_id] = None
        while len(self._seen) > _SEEN_CAP:
            self._seen.pop(next(iter(self._seen)))

    async def list_open(self) -> dict[str, Any]:
        reply = await self._fetch()
        rows = _rows(reply)
        if rows is None:
            return daemon_error(reply) if not reply.ok else {"error": "no approval queue"}
        for row in rows:
            self._remember(str(row["id"]))
        approvals = [_public(row) for row in rows]
        return {"count": len(approvals), "approvals": approvals}

    async def respond(self, approval_id: object, decision: object) -> dict[str, Any]:
        wanted = approval_id.strip() if isinstance(approval_id, str) else ""
        name = decision.strip().lower() if isinstance(decision, str) else ""
        choice = wire.DECISIONS.get(name)
        if choice is None:
            return {"error": f"decision must be one of {', '.join(DECISION_NAMES)}"}
        if not wanted:
            return {"error": "an approval id is required"}
        reply = await self._fetch()
        rows = _rows(reply)
        if rows is None:
            return daemon_error(reply) if not reply.ok else {"error": "no approval queue"}
        row = next((r for r in rows if r["id"] == wanted), None)
        settled = {"resolved": False, "approval_id": wanted, "reason": wire.REASON_SETTLED}
        if row is None:
            if wanted in self._seen or wanted in self._answered:
                return settled
            return {"error": f"approval not found: {wanted}"}
        self._remember(wanted)
        generation = row.get("generation")
        home = self.scope.home
        session_id = str(row.get("session_id") or "")
        sent = await self._offload(
            lambda: self._answer(
                home,
                session_id=session_id,
                request_id=wanted,
                generation=generation if isinstance(generation, int) else None,
                choice=choice,
            )
        )
        if not sent.ok or sent.result is None:
            return daemon_error(sent)
        if sent.result.get("resolved") is True:
            self._answered[wanted] = name
            return {"resolved": True, "approval_id": wanted, "decision": name}
        reason = str(sent.result.get("reason") or wire.REASON_SETTLED)
        return {"resolved": False, "approval_id": wanted, "reason": reason}

    async def watch(self) -> None:
        """Turn queue changes into bridge events (see the module notes)."""
        if not self.available or self.bridge is None:
            return
        now = self.clock.monotonic()
        if self._next_watch is not None and now < self._next_watch:
            return
        self._next_watch = now + WATCH_SECONDS
        try:
            reply = await self._fetch()
        except Exception:
            log.debug("approval queue read failed", exc_info=True)
            return
        rows = _rows(reply)
        if rows is None:
            return
        current = {str(row["id"]): row for row in rows}
        for approval_id, row in current.items():
            if approval_id not in self._known:
                self._remember(approval_id)
                self.bridge.publish(
                    "approval_requested",
                    str(row.get("session_key") or ""),
                    {
                        "approval_id": approval_id,
                        "session_id": row.get("session_id"),
                        "command": row.get("command", ""),
                    },
                )
        for approval_id, row in self._known.items():
            if approval_id not in current:
                data: dict[str, Any] = {"approval_id": approval_id}
                if approval_id in self._answered:
                    data["decision"] = self._answered[approval_id]
                self.bridge.publish("approval_resolved", str(row.get("session_key") or ""), data)
        self._known = current
