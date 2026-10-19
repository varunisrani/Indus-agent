"""Approval and clarify questions over chat (P15-22, E8).

``ChatPrompts`` is the admission pipeline's ``on_request`` hook and one of its front doors. When an
agent turn asks a person something (an ``approval`` or ``clarify`` server request), it opens a
card in the chat the turn came from and turns that person's reply into the answer.

Approval card (wording: ``service.approval_copy``): a header, the command masked on the server
(P05 redactor, always forced) and wrapped (P05 ``display_command``), at most ten lines and a
"+N more lines" count (R05.22), why it was flagged, and a line saying that no answer means it does
not run. Buttons for the offered choices where the platform has them, else the same card with the
``/approve`` / ``/deny`` hint. A button card that cannot be sent falls back to the text card; a
request nothing could be shown for is denied at once with that reason.

Answers: a button press, or a typed ``/approve [once|session|always|all]`` / ``/deny [reason]``,
reach the front door in the adapter's own await, ahead of both busy guards (R06.1). They are taken
only from the person whose message started the turn, or from an admin of the chat's tier list
(P15-10, when one is configured), after the authorization chain (P15-08); bots never answer. A bare
"yes" is not an answer: it reaches the agent as text (R06.6).

Endings, each worded differently (R02.5): approved, denied (not run), timed out (not run),
withdrawn (the reply stopped; nothing ran), answered elsewhere. A button card is edited in place
(its buttons go) where the platform can edit, else a notice is posted. The broker owns the
deadline it was given (P05 then reports a timeout, never a denial); the gateway only denies on its
own when the request carries no deadline (after ``approvals.timeout``) or the broker missed its
own by ``GRACE_S``.

Clarify: one question at a time; choices numbered (buttons where available), paged at
``PAGE_SIZE`` with "more", an "Other" free answer and, for multi-select, "Done". Typed answers:
see ``parse_choice_reply``. While a question is open the asker's next plain message is the answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from indus.gateway.adapters.base import (
    ApprovalAction,
    ApprovalPrompt,
    ClarifyPrompt,
    parse_approval_callback,
    parse_clarify_callback,
)
from indus.gateway.core.busy import canonical_command
from indus.gateway.core.events import InboundEvent, Source, reply_thread
from indus.protocol.events import CancelReason, ServerRequestCancelled
from indus.protocol.methods import ProtocolError
from indus.protocol.server_requests import (
    ApprovalAnswer,
    ApprovalChoice,
    ApprovalRequest,
    ClarifyAnswer,
    ClarifyRequest,
)
from indus.safety.approvals.human import approval_timeout, display_command
from indus.safety.redaction import redact_output
from indus.service import approval_copy as copy

if TYPE_CHECKING:
    from indus.foundation.scope import Scope
    from indus.gateway.adapters.base import Adapter
    from indus.gateway.core.admission import AdmissionPipeline
    from indus.protocol.base import ProtocolModel

__all__ = [
    "GRACE_S",
    "MAX_COMMAND_LINES",
    "PAGE_SIZE",
    "Card",
    "ChatPrompts",
    "approval_text",
    "command_lines",
    "is_chat_admin",
    "may_answer",
    "parse_choice_reply",
    "requester_of",
]

log = logging.getLogger(__name__)

MAX_COMMAND_LINES = 10
PAGE_SIZE = 10
GRACE_S = 2.0
LABEL_MAX = 60

_APPROVE_WORDS: dict[str, ApprovalChoice] = {
    "": ApprovalChoice.ONCE,
    "once": ApprovalChoice.ONCE,
    "session": ApprovalChoice.SESSION,
    "always": ApprovalChoice.ALWAYS,
}
_USAGE = "Usage: /approve, /approve session, /approve always, /approve all, or /deny [reason]"


# -- rendering -------------------------------------------------------------------------------------


def command_lines(command: str, scope: Scope | None) -> list[str]:
    """The command as shown: masked, wrapped, at most ``MAX_COMMAND_LINES`` lines + a count."""
    masked = redact_output(command or "", force=True, scope=scope)
    lines = display_command(masked).split("\n")
    lines = [line.replace("```", "'''") for line in lines]  # never close the card's fence
    if len(lines) > MAX_COMMAND_LINES:
        rest = len(lines) - MAX_COMMAND_LINES
        lines = [*lines[:MAX_COMMAND_LINES], copy.more_lines(rest)]
    return lines


def approval_text(
    request: ApprovalRequest, scope: Scope | None, deadline_s: float, *, buttons: bool
) -> str:
    """The approval card; without buttons it ends with the typed-answer hint."""
    reason = redact_output(request.description or "", force=True, scope=scope).strip()
    parts = [
        copy.APPROVAL_HEADER,
        "```",
        *command_lines(request.command, scope),
        "```",
        f"{copy.REASON_LABEL}: {reason or 'it matched a risky pattern'}",
        copy.deadline_line(deadline_s),
    ]
    if not buttons:
        parts.append(copy.text_hint(str(c) for c in request.choices))
    return "\n".join(parts)


def parse_choice_reply(text: str, choices: Sequence[str], multi: bool) -> list[str] | None:
    """A typed answer to one clarify question: the picked options or the free text; ``None`` for
    a number that names no option (the question stays open).

    Single-select: a number, an option's text (any case) or free text. Multi-select: numbers
    separated by commas or spaces, options' texts separated by commas, or free text."""
    stripped = text.strip()
    if not choices:
        return [stripped]
    by_text = {c.strip().lower(): c for c in choices}
    if stripped.lower() in by_text:
        return [by_text[stripped.lower()]]
    if multi:
        parts = [p for p in re.split(r"[,\s]+", stripped) if p]
        if parts and all(p.rstrip(".)").isdigit() for p in parts):
            numbers = [int(p.rstrip(".)")) for p in parts]
            if any(n < 1 or n > len(choices) for n in numbers):
                return None
            return list(dict.fromkeys(choices[n - 1] for n in numbers))
        pieces = [p.strip().lower() for p in stripped.split(",") if p.strip()]
        if len(pieces) > 1 and all(p in by_text for p in pieces):
            return list(dict.fromkeys(by_text[p] for p in pieces))
        return [stripped]
    number = stripped.rstrip(".)")
    if number.isdigit():
        n = int(number)
        return [choices[n - 1]] if 1 <= n <= len(choices) else None
    return [stripped]


def _label(text: str) -> str:
    one = " ".join(text.split())
    return one if len(one) <= LABEL_MAX else one[: LABEL_MAX - 1] + "…"


def _who(source: Source) -> str | None:
    """The person a source names: the user id, or the chat id of a DM."""
    if source.user_id:
        return str(source.user_id)
    if source.chat_type == "dm" and source.chat_id:
        return str(source.chat_id)
    return None


def requester_of(source: Source | None) -> str | None:
    """Who may answer as the requester of a turn started from ``source``."""
    return _who(source) if source is not None else None


def is_chat_admin(pipeline: AdmissionPipeline, source: Source) -> bool:
    """An admin of the chat's tier list; tiers off means nobody is (the requester rule holds)."""
    try:
        policy = pipeline.commands.policy(source)
    except Exception:
        return False
    return policy.enabled and bool(source.user_id) and str(source.user_id) in policy.admins


def may_answer(pipeline: AdmissionPipeline, answerer: Source, requester: str | None) -> bool:
    """The one rule for answering an approval or a question in a chat: never a bot; the person
    whose message started the turn (``requester``), or an admin of the chat's tier list."""
    if answerer.is_bot:
        return False
    who = _who(answerer)
    if who is not None and who == requester:
        return True
    return is_chat_admin(pipeline, answerer)


def _same_chat(a: Source, b: Source) -> bool:
    return (
        str(a.platform).lower() == str(b.platform).lower()
        and str(a.chat_id) == str(b.chat_id)
        and (a.thread_id or None) == (b.thread_id or None)
    )


def _meta(source: Source, adapter: Adapter) -> dict[str, Any]:
    """Where a card goes: the source's thread, or (``reply_opens_thread``: Slack) the
    prospective thread the conversation is keyed on, so a channel card lands in the thread."""
    thread = reply_thread(source, opens_thread=adapter.capabilities.reply_opens_thread)
    return {"thread_id": thread} if thread else {}


# -- cards -----------------------------------------------------------------------------------------


@dataclass
class Card:
    """One open question in one chat."""

    request: ApprovalRequest | ClarifyRequest
    key: str
    source: Source
    requester: str | None
    deadline_s: float | None
    message_id: str | None = None
    buttons: bool = False
    closed: bool = False
    timer: asyncio.Task[None] | None = None
    # clarify state
    index: int = 0
    page: int = 0
    answers: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    picked: list[str] = field(default_factory=list[str])
    other: bool = False
    actions: list[tuple[str, int]] = field(default_factory=list[tuple[str, int]])

    @property
    def request_id(self) -> str:
        return self.request.request_id

    @property
    def session_id(self) -> str:
        return self.request.session_id

    @property
    def is_approval(self) -> bool:
        return isinstance(self.request, ApprovalRequest)


class ChatPrompts:
    """Approval and clarify cards of one intake (see the module notes)."""

    def __init__(
        self,
        pipeline: AdmissionPipeline,
        *,
        timeout: Callable[[], float] | None = None,
    ) -> None:
        self.pipeline = pipeline
        self._timeout = timeout or (lambda: approval_timeout(pipeline.scope))
        self._cards: dict[str, Card] = {}
        self._watching: dict[str, Callable[[], None]] = {}
        self._closed = False

    # -- wiring ------------------------------------------------------------------------------------

    def install(self) -> Callable[[], None]:
        """Become the pipeline's ``on_request`` hook and a front door; returns the undo."""
        pipeline = self.pipeline
        previous = pipeline.on_request
        pipeline.on_request = self.deliver
        pipeline.front_doors.append(self.front_door)

        def undo() -> None:
            if pipeline.on_request == self.deliver:
                pipeline.on_request = previous
            with contextlib.suppress(ValueError):
                pipeline.front_doors.remove(self.front_door)

        return undo

    async def aclose(self) -> None:
        self._closed = True
        for card in list(self._cards.values()):
            card.closed = True
            if card.timer is not None and not card.timer.done():
                card.timer.cancel()
        self._cards.clear()
        for unsubscribe in self._watching.values():
            with contextlib.suppress(Exception):
                unsubscribe()
        self._watching.clear()

    def open_cards(self, key: str | None = None) -> list[Card]:
        """Open cards, oldest first (all, or one conversation's)."""
        return [c for c in self._cards.values() if key is None or c.key == key]

    @property
    def _scope(self) -> Scope:
        return self.pipeline.scope

    def _adapter(self, source: Source) -> Adapter | None:
        return self.pipeline.adapters.get(str(source.platform).lower())

    # -- opening -----------------------------------------------------------------------------------

    async def deliver(self, source: Source, request: ProtocolModel) -> None:
        """The pipeline's ``on_request`` hook: open a card for an approval or clarify request."""
        if self._closed:
            return
        if isinstance(request, ApprovalRequest):
            await self._open_approval(source, request)
        elif isinstance(request, ClarifyRequest):
            await self._open_clarify(source, request)

    def _new_card(self, source: Source, request: ApprovalRequest | ClarifyRequest) -> Card:
        card = Card(
            request,
            self.pipeline.session_key(source),
            source,
            _who(source),
            request.deadline_s,
        )
        self._cards[request.request_id] = card
        self._watch(request.session_id)
        return card

    async def _open_approval(self, source: Source, request: ApprovalRequest) -> None:
        card = self._new_card(source, request)
        window = request.deadline_s or self._timeout()
        text_card = approval_text(request, self._scope, window, buttons=False)
        adapter = self._adapter(source)
        shown = False
        if adapter is not None and adapter.capabilities.supports_buttons:
            actions = tuple(
                ApprovalAction(
                    copy.ACTION_LABELS[choice.value],
                    choice.value,
                    "danger" if choice is ApprovalChoice.DENY else "",
                )
                for choice in request.choices
            )
            prompt = ApprovalPrompt(
                request.request_id,
                card.key,
                approval_text(request, self._scope, window, buttons=True),
                text_card,
                actions,
            )
            result = await adapter.send_approval(
                str(source.chat_id), prompt, _meta(source, adapter)
            )
            if result.ok:
                shown, card.buttons, card.message_id = True, True, result.message_id
            else:
                log.warning("approval buttons failed on %s: %s", source.platform, result.error)
        if not shown:
            shown, card.message_id = await self._send_text(source, text_card)
        if card.closed:
            return
        if not shown:
            await self._undeliverable(card)
            return
        wait = request.deadline_s + GRACE_S if request.deadline_s else window
        card.timer = self.pipeline.spawn(self._expire_after(card, wait), name="approval timer")

    async def _open_clarify(self, source: Source, request: ClarifyRequest) -> None:
        card = self._new_card(source, request)
        if not await self._ask(card):
            await self._undeliverable(card)
            return
        if request.deadline_s and not card.closed:
            card.timer = self.pipeline.spawn(
                self._expire_after(card, request.deadline_s + GRACE_S), name="clarify timer"
            )

    async def _send_text(self, source: Source, text: str) -> tuple[bool, str | None]:
        try:
            report = await self.pipeline.sender.reply(source, text, kind="notice")
        except Exception:
            log.warning("a prompt could not be sent on %s", source.platform, exc_info=True)
            return False, None
        if getattr(report, "outcome", None) != "sent":
            return False, None
        ids = getattr(report, "message_ids", None) or []
        return True, (str(ids[0]) if ids else None)

    async def _undeliverable(self, card: Card) -> None:
        """Nothing could be shown: answer at once so the agent never waits on an unseen card."""
        request = card.request
        self._forget(card)
        try:
            if isinstance(request, ApprovalRequest):
                answer: ApprovalAnswer | ClarifyAnswer = ApprovalAnswer(
                    choice=ApprovalChoice.DENY, reason=copy.NOT_SHOWN_REASON
                )
            else:
                answer = ClarifyAnswer(
                    answers={q.id: [copy.CLARIFY_NOT_ASKED] for q in request.questions}
                )
            self.pipeline.service.answer_server_request(card.session_id, card.request_id, answer)
        except ProtocolError:
            pass
        log.warning("a %s prompt could not be shown on %s", request.kind, card.source.platform)

    # -- clarify questions -------------------------------------------------------------------------

    def _question_text(self, card: Card) -> tuple[str, list[str]]:
        """The current question's text and its button labels (actions stored on the card)."""
        request = card.request
        assert isinstance(request, ClarifyRequest)
        questions = request.questions
        question = questions[card.index]
        header = copy.CLARIFY_HEADER
        if len(questions) > 1:
            header += f" ({card.index + 1}/{len(questions)})"
        lines = [header, question.text]
        labels: list[str] = []
        card.actions = []
        choices = question.choices or []
        if choices:
            start = card.page * PAGE_SIZE
            chunk = choices[start : start + PAGE_SIZE]
            for offset, choice in enumerate(chunk):
                lines.append(f"{start + offset + 1}. {choice}")
                labels.append(_label(choice))
                card.actions.append(("pick", start + offset))
            more = start + PAGE_SIZE < len(choices)
            if more:
                labels.append(copy.MORE_LABEL)
                card.actions.append(("more", 0))
            if question.multi_select:
                labels.append(copy.DONE_LABEL)
                card.actions.append(("done", 0))
            labels.append(copy.OTHER_LABEL)
            card.actions.append(("other", 0))
            lines.append(copy.clarify_hint(multi=question.multi_select, more=more))
            if question.multi_select and card.picked:
                lines.append(copy.picked_so_far(card.picked))
        else:
            lines.append(copy.CLARIFY_FREE_HINT)
        if card.deadline_s:
            lines.append(copy.clarify_deadline_line(card.deadline_s))
        return "\n".join(lines), labels

    async def _ask(self, card: Card) -> bool:
        """Show the current question (a fresh message); False when nothing could be shown."""
        text, labels = self._question_text(card)
        source = card.source
        adapter = self._adapter(source)
        request = card.request
        assert isinstance(request, ClarifyRequest)
        question = request.questions[card.index]
        card.buttons = False
        if adapter is not None and adapter.capabilities.supports_buttons and labels:
            prompt = ClarifyPrompt(
                request.request_id,
                card.key,
                text,
                tuple(labels),
                question.multi_select,
                text,
            )
            result = await adapter.send_clarify(str(source.chat_id), prompt, _meta(source, adapter))
            if result.ok:
                card.buttons, card.message_id = True, result.message_id
                return True
            log.warning("clarify buttons failed on %s: %s", source.platform, result.error)
        shown, card.message_id = await self._send_text(source, text)
        return shown

    async def _record(self, card: Card, answer: list[str]) -> str | None:
        """Store the current question's answer; ask the next one or send the whole answer.
        Returns what to tell the person (None: the next question was shown)."""
        request = card.request
        assert isinstance(request, ClarifyRequest)
        question = request.questions[card.index]
        card.answers[question.id] = answer
        await self._retire_buttons(card, f"{question.text}\n→ {', '.join(answer) or '(skipped)'}")
        card.index += 1
        card.page, card.picked, card.other = 0, [], False
        if card.index < len(request.questions):
            if await self._ask(card):
                return None
            await self._undeliverable(card)
            return copy.NOT_WAITING
        self._forget(card)
        try:
            self.pipeline.service.answer_server_request(
                card.session_id, card.request_id, ClarifyAnswer(answers=card.answers)
            )
        except ProtocolError:
            return copy.NOT_WAITING
        return copy.CLARIFY_DONE

    async def _retire_buttons(self, card: Card, text: str) -> None:
        """Edit a button card so its buttons go (where the platform can edit)."""
        if not (card.buttons and card.message_id):
            return
        adapter = self._adapter(card.source)
        if adapter is None or not adapter.capabilities.supports_edit:
            return
        with contextlib.suppress(Exception):
            await adapter.edit(
                str(card.source.chat_id),
                card.message_id,
                text,
                final=True,
                meta=_meta(card.source, adapter),
            )
        card.buttons = False

    # -- endings -----------------------------------------------------------------------------------

    def _forget(self, card: Card) -> None:
        card.closed = True
        self._cards.pop(card.request_id, None)
        timer = card.timer
        if timer is not None and timer is not asyncio.current_task() and not timer.done():
            timer.cancel()

    async def _close(self, card: Card, text: str, *, notify: bool) -> None:
        """End a card once: edit a button card in place, else post ``text`` when ``notify``."""
        if card.closed:
            return
        self._forget(card)
        edited = False
        if card.buttons and card.message_id:
            adapter = self._adapter(card.source)
            if adapter is not None and adapter.capabilities.supports_edit:
                header = copy.APPROVAL_HEADER if card.is_approval else copy.CLARIFY_HEADER
                result = await adapter.edit(
                    str(card.source.chat_id),
                    card.message_id,
                    f"{header}\n{text}",
                    final=True,
                    meta=_meta(card.source, adapter),
                )
                edited = result.ok
        if notify and not edited:
            await self.pipeline.reply(card.source, text, kind="notice")

    async def _expire_after(self, card: Card, seconds: float) -> None:
        await self.pipeline.clock.sleep(seconds)
        if card.closed:
            return
        if card.is_approval:
            with contextlib.suppress(ProtocolError):
                self.pipeline.service.answer_server_request(
                    card.session_id,
                    card.request_id,
                    ApprovalAnswer(choice=ApprovalChoice.DENY, reason="no answer in time"),
                )
            window = card.deadline_s or self._timeout()
            await self._close(card, copy.timed_out(window), notify=True)
        else:
            await self._close(card, copy.CLARIFY_TIMED_OUT, notify=True)

    def _watch(self, session_id: str) -> None:
        if session_id in self._watching:
            return
        try:
            self._watching[session_id] = self.pipeline.service.subscribe(session_id, self._on_event)
        except ProtocolError:
            log.debug("could not watch %s for withdrawn requests", session_id)

    def _on_event(self, event: object) -> None:
        if not isinstance(event, ServerRequestCancelled):
            return
        card = self._cards.get(event.request_id)
        if card is not None:
            self.pipeline.spawn(self._withdrawn(card, event.reason), name="request withdrawn")

    async def _withdrawn(self, card: Card, reason: CancelReason) -> None:
        if reason is CancelReason.TIMED_OUT:
            if card.is_approval:
                text = copy.timed_out(card.deadline_s or self._timeout())
            else:
                text = copy.CLARIFY_TIMED_OUT
        elif card.is_approval:
            text = copy.withdrawn(reason.value)
        else:
            text = copy.clarify_withdrawn(reason.value)
        await self._close(card, text, notify=True)

    # -- answers -----------------------------------------------------------------------------------

    def _admitted(self, source: Source) -> bool:
        if source.is_bot:
            return False
        try:
            return self.pipeline.authorizer.check(source).granted
        except Exception:
            log.warning("authorization failed for a prompt answer", exc_info=True)
            return False

    def _is_admin(self, source: Source) -> bool:
        return is_chat_admin(self.pipeline, source)

    def _may_answer(self, source: Source, card: Card) -> bool:
        return may_answer(self.pipeline, source, card.requester)

    async def front_door(self, event: InboundEvent) -> bool:
        """Take button presses for our cards, typed approvals for open cards and the asker's
        answer to an open question; everything else goes on as usual."""
        if event.internal or self._closed:
            return False
        data = event.metadata.get("button")
        if isinstance(data, str) and data:
            return await self._button(event, data)
        if not event.allow_control:
            return False
        name = canonical_command(event)
        if name in ("approve", "deny"):
            return await self._typed_approval(event, name)
        if event.is_command:
            return False
        return await self._typed_clarify(event)

    async def _ack(self, event: InboundEvent, text: str) -> None:
        await self.pipeline.reply(event.source, text, reply_to=event.message_id, kind="ack")

    async def _button(self, event: InboundEvent, data: str) -> bool:
        approval = parse_approval_callback(data)
        clarify = parse_clarify_callback(data)
        if approval is None and clarify is None:
            return False
        request_id = approval[0] if approval is not None else clarify[0] if clarify else ""
        if not self._admitted(event.source):
            log.info("ignored a prompt button from %s", event.source.user_id or "?")
            return True
        card = self._cards.get(request_id)
        if card is None or card.closed or not _same_chat(card.source, event.source):
            await self._ack(event, copy.NOT_WAITING)
            return True
        if not self._may_answer(event.source, card):
            await self._ack(event, copy.NOT_REQUESTER)
            return True
        if approval is not None:
            if not card.is_approval:
                return True
            choice = ApprovalChoice(approval[1])
            if choice not in card.request.choices:  # type: ignore[union-attr]
                return True
            await self._answer_approval(card, choice, None, notify=True)
            return True
        assert clarify is not None
        if card.is_approval:
            return True
        reply = await self._clarify_button(card, clarify[1])
        if reply:
            await self._ack(event, reply)
        return True

    async def _answer_approval(
        self, card: Card, choice: ApprovalChoice, reason: str | None, *, notify: bool
    ) -> str:
        if card.closed:
            return copy.NOT_WAITING
        try:
            self.pipeline.service.answer_server_request(
                card.session_id, card.request_id, ApprovalAnswer(choice=choice, reason=reason)
            )
        except ProtocolError:
            self._forget(card)
            return copy.NOT_WAITING
        text = copy.denied(reason) if choice is ApprovalChoice.DENY else copy.approved(choice.value)
        await self._close(card, text, notify=notify)
        return text

    async def _typed_approval(self, event: InboundEvent, name: str) -> bool:
        source = event.source
        key = self.pipeline.session_key(source)
        cards = [c for c in self.open_cards(key) if c.is_approval]
        if not cards and self._is_admin(source):
            cards = [c for c in self.open_cards() if c.is_approval and _same_chat(c.source, source)]
        if not cards:
            return False  # the command's own handler answers "nothing is waiting"
        if not await self.pipeline.authorize(event):
            return True
        allowed = [c for c in cards if self._may_answer(source, c)]
        if not allowed:
            await self.pipeline.reply(source, copy.NOT_REQUESTER, reply_to=event.message_id)
            return True
        args = event.command_args.strip()
        if name == "deny":
            text = await self._answer_approval(
                allowed[0], ApprovalChoice.DENY, args or None, notify=False
            )
            await self.pipeline.reply(source, text, reply_to=event.message_id)
            return True
        word = args.lower()
        if word not in _APPROVE_WORDS and word != "all":
            await self.pipeline.reply(source, _USAGE, reply_to=event.message_id)
            return True
        choice = ApprovalChoice.ONCE if word == "all" else _APPROVE_WORDS[word]
        targets = allowed if word == "all" else allowed[:1]
        first = targets[0].request
        assert isinstance(first, ApprovalRequest)
        if choice not in first.choices:
            offered = [c.value for c in first.choices]
            await self.pipeline.reply(
                source, copy.not_offered(word, offered), reply_to=event.message_id
            )
            return True
        done, last = 0, copy.NOT_WAITING
        for card in targets:
            last = await self._answer_approval(card, choice, None, notify=False)
            if last != copy.NOT_WAITING:
                done += 1
        text = copy.approved_many(done) if word == "all" and done else last
        await self.pipeline.reply(source, text, reply_to=event.message_id)
        return True

    async def _clarify_button(self, card: Card, index: int) -> str | None:
        if index >= len(card.actions):
            return copy.NOT_WAITING
        kind, value = card.actions[index]
        request = card.request
        assert isinstance(request, ClarifyRequest)
        question = request.questions[card.index]
        choices = question.choices or []
        if kind == "pick" and value < len(choices):
            choice = choices[value]
            if not question.multi_select:
                return await self._record(card, [choice])
            if choice in card.picked:
                card.picked.remove(choice)
            else:
                card.picked.append(choice)
            return copy.picked_so_far(card.picked)
        if kind == "done":
            return await self._record(card, list(card.picked))
        if kind == "more":
            return await self._next_page(card)
        if kind == "other":
            card.other = True
            return copy.OTHER_PROMPT
        return None

    async def _next_page(self, card: Card) -> str | None:
        request = card.request
        assert isinstance(request, ClarifyRequest)
        count = len(request.questions[card.index].choices or [])
        pages = max(1, -(-count // PAGE_SIZE))
        card.page = (card.page + 1) % pages
        await self._retire_buttons(card, request.questions[card.index].text)
        if await self._ask(card):
            return None
        await self._undeliverable(card)
        return copy.NOT_WAITING

    async def _typed_clarify(self, event: InboundEvent) -> bool:
        source = event.source
        key = self.pipeline.session_key(source)
        card = next((c for c in self.open_cards(key) if not c.is_approval), None)
        if card is None or not self._admitted(source) or not self._may_answer(source, card):
            return False
        text = event.text.strip()
        if not text:
            return False
        request = card.request
        assert isinstance(request, ClarifyRequest)
        question = request.questions[card.index]
        choices = question.choices or []
        if card.other:
            reply = await self._record(card, [text])
        elif choices and text.lower() == "more":
            reply = await self._next_page(card)
        else:
            answer = parse_choice_reply(text, choices, question.multi_select)
            if answer is None:
                reply = copy.out_of_range(len(choices))
            else:
                reply = await self._record(card, answer)
        if reply:
            await self.pipeline.reply(source, reply, reply_to=event.message_id, kind="ack")
        return True
