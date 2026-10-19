"""Mixture-of-agents turns (P10-21).

A host arms a session's next turn (``/moa [preset] <prompt>``) or every turn (``/moa on``) in
``ARMED``; the loop claims the plan right after the prologue (``claim``) and, before each request
of that turn, calls ``MoaTurn.before_request``:

1. the request's messages become a reference view: no system prompt, plain user and assistant
   text, tool calls as one-line text, each tool result folded into the assistant text before it and
   cut to ``TOOL_RESULT_CHARS`` (head and tail); the view always ends on a user message;
2. every enabled reference of the preset is asked at once (``REFERENCE_WORKERS`` at a time), with
   our own reference instructions first and the view trimmed to that model's window minus
   ``OUTPUT_RESERVE`` and a ``SAFETY_FRACTION`` margin; a per-reference deadline on the turn's clock
   (none by default);
3. the answers become one labelled block, appended to the request's last user message — the copy
   sent, never the stored row, never the system prompt. A failed or timed-out reference is named in
   the block with why; the turn goes on either way.

The session's own model stays the aggregator: it calls the tools and ends the turn. MoA turns are
the documented exception to byte-stable prompts (D-16.72). Reference calls go through the host's
auxiliary client on their own routes (``AuxClient.call_route``), so their usage is in the session
ledger under the ``moa`` task.

Alternation (R01.2): a destination that rejects two messages of one role in a row
(``role_alternation``) is asked again once with neighbours merged and remembered, so every later
list for it is merged before sending.

Interrupt (R01.4): the wait wakes on the turn's stop event, lets calls that are finishing land for
``SETTLE_S``, then cancels the rest; their slots say they were skipped. ``POLL_S`` bounds how long
the wait goes without looking at the control.

Privacy (``moa.privacy_filter``): ``display`` masks emails and formatted phone numbers (after the
main secret redactor) in the reference text shown as notices; ``full`` also in the block the model
gets.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

from indus.core.compression.anchor import estimate_message
from indus.core.moa_presets import Preset, PrivacyMode, Slot, fanout_runs
from indus.foundation.clock import Clock
from indus.protocol.events import Notice, NoticeLevel, Status, StatusKind
from indus.providers.types import ChatRequest

if TYPE_CHECKING:
    from indus.core.turn_state import TurnState
    from indus.foundation.scope import Scope
    from indus.providers.auxiliary import AuxClient

__all__ = [
    "ARMED",
    "OUTPUT_RESERVE",
    "POLL_S",
    "REFERENCE_WORKERS",
    "SAFETY_FRACTION",
    "SETTLE_S",
    "TASK",
    "TOOL_RESULT_CHARS",
    "AuxReferences",
    "MoaPlan",
    "MoaTable",
    "MoaTurn",
    "ReferenceAnswer",
    "ReferenceCaller",
    "attach_block",
    "claim",
    "clip_result",
    "gather",
    "mask_private",
    "merge_alternation",
    "reference_view",
    "render_block",
    "trim_for_window",
]

log = logging.getLogger(__name__)

TASK: Final = "moa"
REFERENCE_WORKERS: Final = 8
TOOL_RESULT_CHARS: Final = 4_000
OUTPUT_RESERVE: Final = 8_192
SAFETY_FRACTION: Final = 0.10
POLL_S: Final = 5.0
SETTLE_S: Final = 0.05

REFERENCE_INSTRUCTIONS: Final = (
    "You are advising another assistant that is in the middle of the conversation below. Only "
    "that assistant can run tools, read files or open links; you cannot, so do not pretend you "
    "did and do not write tool calls. Read where the work stands and say plainly what should "
    "happen next: the approach you would take, concrete next steps, risks, and anything that "
    "looks wrong or missed. Your answer goes only to that assistant, not to the user. Be direct "
    "and skip disclaimers."
)
ADVICE_REQUEST: Final = (
    "(The conversation above is where the work stands now. What should the assistant do next, "
    "and what should it watch out for?)"
)
BLOCK_HEAD: Final = (
    "[Advice from {count} other model(s) on this step. It is private input for you, not a "
    "message from the user: weigh it, act with your own tools and judgement, and answer the user "
    "yourself.]"
)
NO_ADVICE: Final = "[No other model answered on this step ({why}); go on without their advice.]"
EARLIER_ADVICE: Final = (
    "[This advice was given earlier in this turn, before the tool results that follow; a step it "
    "suggests may already be done, so check the results before repeating anything.]\n"
)
SKIPPED_INTERRUPTED: Final = "skipped: the turn was interrupted"
NOTICE_CHARS: Final = 600

_EMAIL = re.compile(r"\b[\w.%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
# Formatted phone numbers only: separators or brackets are required, so plain digit runs,
# dates, times, versions and addresses stay as they are.
_PHONE = re.compile(
    r"(?<![\w.+-])(?:\+\d{1,3}[ .-])?(?:\(\d{2,4}\) ?|\d{3}[.-])\d{3}[.-]\d{4}(?![\w-])"
)

type Messages = list[dict[str, Any]]


# -- reference view ------------------------------------------------------------------------------


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in cast(list[object], content):
            if isinstance(part, Mapping):
                item = cast(Mapping[str, Any], part)
                if isinstance(item.get("text"), str):
                    parts.append(cast(str, item["text"]))
        return "\n".join(parts)
    return ""


def clip_result(text: str, limit: int = TOOL_RESULT_CHARS) -> str:
    """``text`` cut to about ``limit`` characters: the head and the tail with a note between."""
    if len(text) <= limit:
        return text
    half = limit // 2
    left_out = len(text) - 2 * half
    return f"{text[:half]}\n[… {left_out} characters left out …]\n{text[-half:]}"


def _as_map(value: object) -> Mapping[str, Any]:
    empty: dict[str, Any] = {}
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else empty


def _call_line(call: object) -> str:
    entry = _as_map(call)
    fn_map = _as_map(entry.get("function"))
    name = str(fn_map.get("name") or entry.get("name") or "a tool")
    args: Any = fn_map.get("arguments", entry.get("arguments"))
    if args is None or args == "":
        return f"[called {name}]"
    if not isinstance(args, str):
        try:
            args = json.dumps(args, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            args = str(args)
    return f"[called {name} with {args}]"


def reference_view(messages: Sequence[Mapping[str, Any]]) -> Messages:
    """The conversation as references see it (see the module notes)."""
    out: Messages = []
    for msg in messages:
        role = msg.get("role")
        text = _text_of(msg.get("content"))
        if role == "user":
            if not text.strip() and isinstance(msg.get("content"), list):
                text = "[the user sent an attachment without text]"
            if text.strip():
                out.append({"role": "user", "content": text})
        elif role == "assistant":
            pieces = [text.strip()] if text.strip() else []
            calls = msg.get("tool_calls")
            if isinstance(calls, list):
                pieces.extend(_call_line(c) for c in cast(list[object], calls))
            if pieces:
                out.append({"role": "assistant", "content": "\n".join(pieces)})
        elif role == "tool":
            name = msg.get("name") or "the tool"
            block = f"[result of {name}: {clip_result(text)}]"
            if out and out[-1]["role"] == "assistant":
                out[-1]["content"] = f"{out[-1]['content']}\n{block}"
            else:
                out.append({"role": "assistant", "content": block})
    if out and out[-1]["role"] == "assistant":
        out.append({"role": "user", "content": ADVICE_REQUEST})
    return out


def merge_alternation(messages: Sequence[Mapping[str, Any]]) -> Messages:
    """``messages`` with neighbours of the same role joined into one (text joined by a blank
    line); the input is not changed."""
    out: Messages = []
    for msg in messages:
        item = dict(msg)
        if out and out[-1].get("role") == item.get("role") and item.get("role") != "system":
            joined = f"{_text_of(out[-1].get('content'))}\n\n{_text_of(item.get('content'))}"
            out[-1] = {**out[-1], "content": joined}
        else:
            out.append(item)
    return out


def trim_for_window(
    view: Sequence[Mapping[str, Any]],
    window: int | None,
    *,
    head: Sequence[Mapping[str, Any]] = (),
    reserve: int = OUTPUT_RESERVE,
) -> Messages:
    """``view`` cut from the oldest end so ``head + view`` fits ``window × (1 − margin) − reserve``
    tokens (rough estimate). The last user message and the one before it are always kept, and the
    kept list never starts on an assistant message. No known window: unchanged."""
    kept = [dict(m) for m in view]
    if not window or window <= 0:
        return kept
    budget = int(window * (1.0 - SAFETY_FRACTION)) - reserve
    weights = [estimate_message(m) for m in kept]
    total = sum(estimate_message(m) for m in head) + sum(weights)
    if total <= budget:
        return kept
    start = 0
    while len(kept) - start > 2 and total > budget:
        total -= weights[start]
        start += 1
    while len(kept) - start > 1 and kept[start].get("role") == "assistant":
        total -= weights[start]
        start += 1
    if start:
        log.info("reference view trimmed by %d message(s) to fit a %d-token window", start, window)
    return kept[start:]


# -- privacy ---------------------------------------------------------------------------------------


def mask_private(text: str, scope: Scope | None = None) -> str:
    """Secrets masked by the main redactor, then emails and formatted phone numbers."""
    from indus.safety.redaction import redact_output

    masked = redact_output(text, force=True, scope=scope)
    masked = _EMAIL.sub("[email hidden]", masked)
    return _PHONE.sub("[phone hidden]", masked)


# -- calling references ----------------------------------------------------------------------------


class ReferenceCaller(Protocol):
    """Asks one reference model; raises when it cannot answer."""

    async def ask(self, slot: Slot, messages: Messages) -> str: ...

    async def window(self, slot: Slot) -> int | None:
        """The model's context window in tokens; None when unknown."""
        ...


class AuxReferences:
    """``ReferenceCaller`` over the host's auxiliary client (P08-17), one route per slot."""

    def __init__(self, aux: AuxClient) -> None:
        self.aux = aux
        self.merged: set[tuple[str, str]] = set()
        self._windows: dict[tuple[str, str], int | None] = {}

    def _route(self, slot: Slot) -> Any:
        from indus.providers.auxiliary import Candidate

        return self.aux.route_for(
            Candidate("pin", slot.provider, slot.model, "", slot.reasoning_effort)
        )

    @staticmethod
    def destination(route: Any) -> tuple[str, str]:
        where = str(getattr(route, "base_url", "") or getattr(route, "provider", ""))
        return where.rstrip("/"), str(getattr(route, "model", ""))

    async def ask(self, slot: Slot, messages: Messages) -> str:
        from indus.providers.retry import RetryFailed

        route = self._route(slot)
        key = self.destination(route)
        sent = merge_alternation(messages) if key in self.merged else messages
        try:
            result = await self.aux.call_route(
                TASK, route, sent, reasoning_effort=slot.reasoning_effort or None
            )
        except RetryFailed as exc:
            if exc.classified.reason != "role_alternation" or key in self.merged:
                raise
            log.info("%s refused two messages of one role in a row; merging from now on", key)
            self.merged.add(key)
            result = await self.aux.call_route(
                TASK,
                route,
                merge_alternation(messages),
                reasoning_effort=slot.reasoning_effort or None,
            )
        return result.text

    async def window(self, slot: Slot) -> int | None:
        key = (slot.provider, slot.model)
        if key in self._windows:
            return self._windows[key]
        from indus.providers import context_length

        try:
            found: int | None = (
                await context_length.find_window(self._route(slot), scope=self.aux.scope)
            ).tokens
        except Exception:
            log.debug("no window known for %s", slot.label, exc_info=True)
            found = None
        self._windows[key] = found
        return found


@dataclass(frozen=True)
class ReferenceAnswer:
    slot: Slot
    text: str | None
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.text is not None and bool(self.text.strip())


def _short(exc: BaseException) -> str:
    detail = str(exc).strip() or type(exc).__name__
    return detail if len(detail) <= 160 else detail[:160] + "…"


async def _race(work: asyncio.Future[Any], clock: Clock, seconds: float) -> bool:
    """True when ``work`` finished within ``seconds`` on ``clock``."""
    timer = asyncio.ensure_future(clock.sleep(seconds))
    try:
        await asyncio.wait({work, timer}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        timer.cancel()
    return work.done()


async def gather(
    slots: Sequence[Slot],
    view: Sequence[Mapping[str, Any]],
    caller: ReferenceCaller,
    *,
    clock: Clock,
    timeout_s: float | None = None,
    stop: asyncio.Event | None = None,
) -> list[ReferenceAnswer]:
    """Ask every slot at once (``REFERENCE_WORKERS`` at a time); one answer per slot, in order."""
    gate = asyncio.Semaphore(REFERENCE_WORKERS)
    head: Messages = [{"role": "system", "content": REFERENCE_INSTRUCTIONS}]

    async def one(slot: Slot) -> ReferenceAnswer:
        async with gate:
            try:
                window = await caller.window(slot)
            except Exception:
                window = None
            messages = head + trim_for_window(view, window, head=head)
            work = asyncio.ensure_future(caller.ask(slot, messages))
            try:
                if timeout_s is not None and not await _race(work, clock, timeout_s):
                    work.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await work
                    return ReferenceAnswer(slot, None, f"timed out after {timeout_s:g} s")
                text = await work
            except asyncio.CancelledError:
                work.cancel()
                raise
            except Exception as exc:
                log.info("reference %s failed: %s", slot.label, exc)
                return ReferenceAnswer(slot, None, f"failed: {_short(exc)}")
            if not text.strip():
                return ReferenceAnswer(slot, None, "answered with no text")
            return ReferenceAnswer(slot, text.strip())

    tasks = [asyncio.ensure_future(one(slot)) for slot in slots]
    if not tasks:
        return []
    pending: set[asyncio.Future[Any]] = set(tasks)
    stopper: asyncio.Future[Any] | None = (
        asyncio.ensure_future(stop.wait()) if stop is not None else None
    )
    interrupted = False
    try:
        while pending:
            waiting: set[asyncio.Future[Any]] = set(pending)
            if stopper is not None:
                waiting.add(stopper)
            await asyncio.wait(waiting, timeout=POLL_S, return_when=asyncio.FIRST_COMPLETED)
            pending = {t for t in pending if not t.done()}
            if pending and stop is not None and stop.is_set():
                interrupted = True
                break
        if interrupted:
            # Calls that are just finishing keep their answers; the rest are cancelled.
            settle = asyncio.ensure_future(clock.sleep(SETTLE_S))
            while pending and not settle.done():
                await asyncio.wait(pending | {settle}, return_when=asyncio.FIRST_COMPLETED)
                pending = {t for t in pending if not t.done()}
            settle.cancel()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if stopper is not None:
            stopper.cancel()
    answers: list[ReferenceAnswer] = []
    for slot, task in zip(slots, tasks, strict=True):
        if task.cancelled() or task.exception() is not None:
            answers.append(ReferenceAnswer(slot, None, SKIPPED_INTERRUPTED))
        else:
            answers.append(task.result())
    return answers


# -- the block -------------------------------------------------------------------------------------


def render_block(
    answers: Sequence[ReferenceAnswer], *, privacy: PrivacyMode = "", scope: Scope | None = None
) -> str:
    """The labelled advice block the model gets (``full`` privacy masks it)."""
    if not any(a.ok for a in answers):
        why = "; ".join(f"{a.slot.label}: {a.note}" for a in answers) or "none configured"
        return NO_ADVICE.format(why=why)
    lines = [BLOCK_HEAD.format(count=sum(1 for a in answers if a.ok))]
    for i, answer in enumerate(answers, 1):
        if answer.ok:
            text = answer.text or ""
            if privacy == "full":
                text = mask_private(text, scope)
            lines.append(f"Reference {i} ({answer.slot.label}):\n{text}")
        else:
            lines.append(f"Reference {i} ({answer.slot.label}): no answer ({answer.note}).")
    return "\n\n".join(lines)


def attach_block(messages: Sequence[Mapping[str, Any]], block: str) -> list[dict[str, Any]]:
    """``messages`` with ``block`` appended to the last user message (a copy; a list content
    gets one more text part). No user message: the block becomes one at the end."""
    out = [dict(m) for m in messages]
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") != "user":
            continue
        content = out[i].get("content")
        if isinstance(content, list):
            parts = [*cast(list[Any], content), {"type": "text", "text": block}]
            out[i] = {**out[i], "content": parts}
        elif isinstance(content, str) and content:
            out[i] = {**out[i], "content": f"{content}\n\n{block}"}
        else:
            out[i] = {**out[i], "content": block}
        return out
    out.append({"role": "user", "content": block})
    return out


def _tools_since_last_user(messages: Sequence[Mapping[str, Any]]) -> bool:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return False
        if msg.get("role") == "tool":
            return True
    return False


# -- plans and turns -------------------------------------------------------------------------------


@dataclass(frozen=True)
class MoaPlan:
    """What a MoA turn runs with: the preset, how to reach references, the deadline per reference
    (None: no limit) and the privacy filter."""

    preset: Preset
    caller: ReferenceCaller
    timeout_s: float | None = None
    privacy: PrivacyMode = ""


class MoaTurn:
    """One turn's MoA state: which iteration it is and the last block (reused when the preset's
    fan-out skips an iteration)."""

    def __init__(self, plan: MoaPlan) -> None:
        self.plan = plan
        self.iteration = 0
        self.block: str | None = None
        self.answers: list[ReferenceAnswer] = []

    async def before_request(self, state: TurnState, request: ChatRequest) -> ChatRequest:
        self.iteration += 1
        plan = self.plan
        slots = plan.preset.enabled_references
        if not slots:
            return request
        asked = self.block is None or fanout_runs(plan.preset.fanout, self.iteration)
        if asked:
            await self._ask(state, request, slots)
        if state.control.interrupted or not self.block:
            return request
        block = self.block
        if not asked and _tools_since_last_user(request.messages):
            block = EARLIER_ADVICE + block
        return replace(request, messages=attach_block(request.messages, block))

    async def _ask(self, state: TurnState, request: ChatRequest, slots: Sequence[Slot]) -> None:
        plan = self.plan
        scope = state.runtime.scope
        state.touch(f"asking {len(slots)} reference model(s)")
        state.emit(
            Status,
            kind=StatusKind.WORKING,
            text=f"Asking {len(slots)} other model(s) ({plan.preset.name})",
        )
        view = reference_view(request.messages)
        answers = await gather(
            slots,
            view,
            plan.caller,
            clock=state.clock,
            timeout_s=plan.timeout_s,
            stop=state.control.stop,
        )
        self.answers = answers
        if state.control.interrupted:
            return
        self.block = render_block(answers, privacy=plan.privacy, scope=scope)
        for i, answer in enumerate(answers, 1):
            shown = answer.text if answer.ok else f"no answer ({answer.note})"
            shown = shown or ""
            if plan.privacy in ("display", "full"):
                shown = mask_private(shown, scope)
            if len(shown) > NOTICE_CHARS:
                shown = shown[:NOTICE_CHARS] + "…"
            state.emit(
                Notice,
                level=NoticeLevel.INFO if answer.ok else NoticeLevel.WARNING,
                text=f"Reference {i} ({answer.slot.label}): {shown}",
                key=f"moa_reference_{i}",
            )


@dataclass
class _Armed:
    plan: MoaPlan
    sticky: bool = False


@dataclass
class MoaTable:
    """Armed MoA plans per ``(profile home, session id)``: a one-turn arm is taken by the next
    turn; a sticky one (``/moa on``) stays until ``disarm``."""

    entries: dict[tuple[str, str], _Armed] = field(default_factory=dict[tuple[str, str], _Armed])

    @staticmethod
    def key(scope: Scope | None, session_id: str) -> tuple[str, str]:
        return (str(scope.home) if scope is not None else "", session_id)

    def arm(self, scope: Scope | None, session_id: str, plan: MoaPlan, *, sticky: bool) -> None:
        self.entries[self.key(scope, session_id)] = _Armed(plan, sticky)

    def disarm(self, scope: Scope | None, session_id: str) -> bool:
        return self.entries.pop(self.key(scope, session_id), None) is not None

    def peek(self, scope: Scope | None, session_id: str) -> tuple[MoaPlan, bool] | None:
        found = self.entries.get(self.key(scope, session_id))
        return None if found is None else (found.plan, found.sticky)

    def take(self, scope: Scope | None, session_id: str) -> MoaPlan | None:
        key = self.key(scope, session_id)
        found = self.entries.get(key)
        if found is None:
            return None
        if not found.sticky:
            del self.entries[key]
        return found.plan

    def move(self, scope: Scope | None, old: str, new: str) -> None:
        """A rotating compaction moved the conversation: the arm follows it."""
        found = self.entries.pop(self.key(scope, old), None)
        if found is not None:
            self.entries[self.key(scope, new)] = found


ARMED = MoaTable()
"""The process's armed plans (hosts arm, the loop claims)."""


def claim(state: TurnState, table: MoaTable | None = None) -> MoaTurn | None:
    """The MoA plan armed for this turn's session, as the turn's ``MoaTurn`` (None: plain turn)."""
    plan = (table or ARMED).take(state.runtime.scope, state.session_id)
    if plan is None:
        return None
    log.info(
        "turn %s is a mixture-of-agents turn (preset %s, %d reference(s))",
        state.turn_id,
        plan.preset.name,
        len(plan.preset.enabled_references),
    )
    return MoaTurn(plan)
