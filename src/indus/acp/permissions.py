"""Approval and clarify requests of held sessions, shown in the editor (P19-11).

``PermissionBridge.offer`` is called (on the loop) for every server request of a held session. An
``approval`` becomes ``session/request_permission`` with the options the request allows; a
``clarify`` becomes form elicitations or cards (``elicitation``). The answer goes back through
``backend.answer(session, request_id, generation, answer)``. Sudo and secret requests are not shown
in an editor (no secret input there): they stay open for a terminal surface.

Option ids are what the answer is mapped by, never the kind (R09.12; ACP has no session kind, so
"Allow for session" carries the ``allow_always`` kind and the id ``allow_session``):

    once → allow_once/allow_once · session → allow_session/allow_always ·
    always → allow_always/allow_always · deny → deny/reject_once ·
    deny (when session or always is offered) → deny_always/reject_always

A card waits until the request's deadline (else ``approvals.timeout``, 300 s) on the scope's clock;
then it closes and the decision is ``timeout``: nothing is answered, the agent's own deadline
withdraws the request ("nobody answered", which is not a refusal). Attached mode: a dismissed card,
a transport loss or an unknown option answers nothing (another viewer may still answer) and the
card's key is released, so a later snapshot shows a new card. Local mode: the editor is the only
viewer, so a dismissed card or an unknown option is a denial. A request withdrawn while its card is
up (answered elsewhere, turn ended, timed out) ends the card. Every card's tool call is closed
``completed`` (allowed) or ``failed``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from indus.acp import elicitation, tool_kinds
from indus.protocol.events import EventBase, ServerRequestCancelled, TurnDone
from indus.protocol.server_requests import (
    ApprovalChoice,
    ApprovalRequest,
    ClarifyRequest,
    ServerRequest,
)

if TYPE_CHECKING:
    from indus.acp.agent import AcpAgent

__all__ = [
    "CHOICE_OF",
    "DEFAULT_TIMEOUT_S",
    "Decision",
    "PermissionBridge",
    "approval_options",
    "approval_tool_call",
]

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S: Final = 300.0
PERMISSION_METHOD: Final = "session/request_permission"

CHOICE_OF: Final[Mapping[str, ApprovalChoice]] = {
    "allow_once": ApprovalChoice.ONCE,
    "allow_session": ApprovalChoice.SESSION,
    "allow_always": ApprovalChoice.ALWAYS,
    "deny": ApprovalChoice.DENY,
    "deny_always": ApprovalChoice.DENY,
}
"""Option id → our approval choice (the only mapping the answer goes through)."""

type DecisionKind = Literal["allowed", "denied", "timeout", "dismissed", "unanswered"]


@dataclass(frozen=True)
class Decision:
    """What became of one card (kept for logs and tests)."""

    request_id: str
    kind: DecisionKind
    choice: str | None = None


def approval_options(choices: Sequence[ApprovalChoice]) -> list[dict[str, Any]]:
    """The card's options for the choices a request allows (see the module notes)."""
    offered = set(choices)
    options: list[dict[str, Any]] = []
    if ApprovalChoice.ONCE in offered:
        options.append({"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"})
    if ApprovalChoice.SESSION in offered:
        options.append(
            {"optionId": "allow_session", "name": "Allow for session", "kind": "allow_always"}
        )
    if ApprovalChoice.ALWAYS in offered:
        options.append({"optionId": "allow_always", "name": "Allow always", "kind": "allow_always"})
    options.append({"optionId": "deny", "name": "Deny", "kind": "reject_once"})
    if ApprovalChoice.SESSION in offered or ApprovalChoice.ALWAYS in offered:
        options.append({"optionId": "deny_always", "name": "Deny always", "kind": "reject_always"})
    return options


def approval_tool_call(
    request: ApprovalRequest, edit: tuple[str, Mapping[str, Any]] | None = None
) -> dict[str, Any]:
    """The pending tool call a card shows: the command, or the diff of a file edit."""
    card_id = f"approval-{request.request_id}"
    if edit is not None:
        name, args = edit
        content = tool_kinds.start_content(name, args) or []
        return {
            "toolCallId": card_id,
            "title": f"Approve edit: {args.get('path', '')}",
            "kind": "edit",
            "status": "pending",
            "content": content,
            "locations": tool_kinds.locations(args),
            "rawInput": {"tool": name, "arguments": dict(args)},
        }
    command, description = request.command, request.description
    text = f"{description}\n$ {command}" if description else f"$ {command}"
    return {
        "toolCallId": card_id,
        "title": f"{description}: {command}" if description else command,
        "kind": "execute",
        "status": "pending",
        "content": [tool_kinds.text_content(text)],
        "rawInput": {"command": command, "description": description},
    }


def _selected(response: object) -> str | None:
    """The option id an editor picked, or None (dismissed, malformed)."""
    if not isinstance(response, Mapping):
        return None
    outcome = cast(Mapping[str, Any], response).get("outcome")
    if not isinstance(outcome, Mapping):
        return None
    picked = cast(Mapping[str, Any], outcome)
    if picked.get("outcome") != "selected":
        return None
    option = picked.get("optionId")
    return option if isinstance(option, str) else None


class _TimedOut(Exception):
    pass


class PermissionBridge:
    """See the module notes. One per editor connection."""

    def __init__(self, agent: AcpAgent) -> None:
        self.agent = agent
        self._cards: dict[tuple[str, str, int | None], asyncio.Task[bool]] = {}
        self.decisions: list[Decision] = []

    # -- entry points (called on the loop by the sink) -------------------------------------------

    def offer(self, key: str, request: ServerRequest, generation: int | None) -> None:
        if not isinstance(request, ApprovalRequest | ClarifyRequest) or self.agent.peer is None:
            return
        card_key = (key, str(request.request_id), generation)
        if card_key in self._cards:
            return
        task: asyncio.Task[bool] = asyncio.ensure_future(self._ask(key, request, generation))
        self._cards[card_key] = task
        task.add_done_callback(lambda done: self._retire(card_key, request, done))

    def on_event(self, key: str, event: EventBase) -> None:
        """A withdrawn request ends its card; a finished turn forgets answered cards."""
        if isinstance(event, ServerRequestCancelled):
            for card_key, task in list(self._cards.items()):
                if card_key[0] == key and card_key[1] == event.request_id and not task.done():
                    task.cancel()
        elif isinstance(event, TurnDone):
            for card_key, task in list(self._cards.items()):
                if card_key[0] == key and task.done():
                    del self._cards[card_key]

    async def aclose(self) -> None:
        tasks = [t for t in self._cards.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._cards.clear()

    def _retire(
        self,
        card_key: tuple[str, str, int | None],
        request: ServerRequest,
        task: asyncio.Task[bool],
    ) -> None:
        """An unanswered card gives its key back (and leaves the offered list), so a later
        snapshot shows a new card; an accepted one keeps it until its turn ends."""
        answered = not task.cancelled() and task.exception() is None and task.result()
        if answered or self._cards.get(card_key) is not task:
            return
        del self._cards[card_key]
        held = self.agent.sessions.get(card_key[0])
        if held is not None:
            held.offered = [
                (seen, gen)
                for seen, gen in held.offered
                if str(seen.request_id) != str(request.request_id)
            ]

    def _record(self, request_id: str, kind: DecisionKind, choice: str | None = None) -> None:
        self.decisions.append(Decision(request_id, kind, choice))
        del self.decisions[:-256]

    # -- talking to the editor --------------------------------------------------------------------

    def _timeout(self, request: ServerRequest) -> float:
        if request.deadline_s is not None:
            return float(request.deadline_s)
        config = self.agent.scope.config
        try:
            value = config.get("approvals.timeout") if config is not None else None
        except Exception:
            value = None
        if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            return float(value)
        return DEFAULT_TIMEOUT_S

    async def _editor(self, method: str, params: Mapping[str, Any], limit: float) -> Any:
        """One request to the editor, ended after ``limit`` seconds of the scope's clock. The
        updates queued before it (the tool call the card is about) reach the editor first."""
        peer = self.agent.peer
        assert peer is not None
        await self.agent.projection.flush()
        asked = asyncio.ensure_future(peer.request(method, params))
        timer = asyncio.ensure_future(self.agent.scope.clock.sleep(limit))
        try:
            await asyncio.wait({asked, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
            answered = asked.done()
            if not answered:
                asked.cancel()
        if not answered:
            raise _TimedOut
        return asked.result()

    def _close(self, key: str, card_id: str, allowed: bool) -> None:
        self.agent.projection.send(
            key,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": card_id,
                "status": "completed" if allowed else "failed",
            },
        )

    def _editor_id(self, key: str) -> str:
        held = self.agent.sessions.get(key)
        return held.editor_id if held is not None else key

    async def _ask(self, key: str, request: ServerRequest, generation: int | None) -> bool:
        try:
            if isinstance(request, ApprovalRequest):
                return await self._approval(key, request, generation)
            if isinstance(request, ClarifyRequest):
                return await self._clarify(key, request, generation)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.info("the editor card for %s was not answered", request.request_id, exc_info=True)
        return False

    # -- approval ---------------------------------------------------------------------------------

    def _edit_of(self, key: str, request: ApprovalRequest) -> tuple[str, Mapping[str, Any]] | None:
        """The open file edit this approval is about (a write's card shows its diff)."""
        if not request.command.startswith("write "):
            return None
        targets = request.command[len("write ") :]
        for opened in reversed(self.agent.projection.open_tools(key)):
            if opened.name not in ("write_file", "patch"):
                continue
            if request.turn_id is not None and opened.turn_id != request.turn_id:
                continue
            path = opened.args.get("path")
            if isinstance(path, str) and path and path in targets:
                return opened.name, opened.args
        return None

    async def _approval(self, key: str, request: ApprovalRequest, generation: int | None) -> bool:
        rid = str(request.request_id)
        options = approval_options(request.choices)
        offered = {str(option["optionId"]) for option in options}
        tool_call = approval_tool_call(request, self._edit_of(key, request))
        card_id = str(tool_call["toolCallId"])
        params = {"sessionId": self._editor_id(key), "toolCall": tool_call, "options": options}
        local = (await self.agent.backend()).mode == "local"
        try:
            response = await self._editor(PERMISSION_METHOD, params, self._timeout(request))
        except _TimedOut:
            self._close(key, card_id, False)
            self._record(rid, "timeout")
            return False
        except asyncio.CancelledError:
            self._close(key, card_id, False)
            raise
        except Exception:
            log.info("the editor did not answer the approval card %s", rid, exc_info=True)
            response = None
        picked = _selected(response)
        if picked is None or picked not in offered:
            if not local:
                self._close(key, card_id, False)
                self._record(rid, "dismissed" if picked is None else "unanswered")
                return False
            picked = "deny"  # the editor is the only viewer here: a dismissal is a no
        choice = CHOICE_OF[picked]
        allowed = choice is not ApprovalChoice.DENY
        self._close(key, card_id, allowed)
        backend = await self.agent.backend()
        accepted = await backend.answer(
            key, rid, generation, {"kind": "approval", "choice": choice.value}
        )
        self._record(rid, "allowed" if allowed else "denied", choice.value)
        return accepted

    # -- clarify ----------------------------------------------------------------------------------

    async def _clarify(self, key: str, request: ClarifyRequest, generation: int | None) -> bool:
        rid = str(request.request_id)
        local = (await self.agent.backend()).mode == "local"
        limit = self._timeout(request)
        answers: dict[str, list[str]] = {}
        for index, question in enumerate(request.questions):
            try:
                if self.agent.form_elicitation:
                    params = elicitation.form_request(self._editor_id(key), question)
                    answer = elicitation.form_answer(
                        await self._editor(elicitation.ELICITATION_METHOD, params, limit)
                    )
                else:
                    answer = await self._clarify_card(key, rid, index, question, limit)
            except _TimedOut:
                self._record(rid, "timeout")
                return False
            except asyncio.CancelledError:
                raise
            except Exception:
                log.info("the editor did not answer the question %s", rid, exc_info=True)
                answer = None
            if answer is None:
                if not local:
                    self._record(rid, "unanswered")
                    return False
                answer = []
            answers[question.id] = answer
        backend = await self.agent.backend()
        accepted = await backend.answer(
            key, rid, generation, {"kind": "clarify", "answers": answers}
        )
        self._record(rid, "allowed")
        return accepted

    async def _clarify_card(
        self, key: str, rid: str, index: int, question: Any, limit: float
    ) -> list[str] | None:
        tool_call, options, picks = elicitation.card(question, f"clarify-{rid}-{index}")
        params = {"sessionId": self._editor_id(key), "toolCall": tool_call, "options": options}
        card_id = str(tool_call["toolCallId"])
        try:
            response = await self._editor(PERMISSION_METHOD, params, limit)
        except BaseException:
            self._close(key, card_id, False)
            raise
        answer = elicitation.card_answer(response, picks)
        with contextlib.suppress(Exception):
            self._close(key, card_id, bool(answer))
        return answer
