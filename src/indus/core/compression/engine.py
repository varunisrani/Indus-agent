"""The context engine: when to compress, how, and what it means for the turn (P08-05, R01.9).

``ContextEngine`` is the interface (``should_compress``, ``compress``, ``on_session_start/end``,
``update_from_response``); ``BuiltinEngine`` is the one shipped engine, chosen by
``context.engine`` (any other name logs and uses the built-in one: plugins never activate by
themselves, P13 adds the plugin path). ``build_engine`` makes one per session.

Gates. Automatic gates — turn-start preflight, pre-request pressure and post-tool (the pressure
check right after a tool round) — ask the token meter (P08-01): ``ok`` and ``wait`` do nothing;
``compress`` tries unless blocked. Blocks: a summary-failure cooldown, the per-turn attempt cap
(``compression.max_attempts``), and the ineffective breaker (two compactions whose next real
reading was still over the trigger; after 300 s one probe is allowed). A provider-proven overflow
(P08-06) bypasses the cooldown and the breaker for one bounded attempt without clearing them; a
manual ``/compress`` clears the cooldown first.

Compress: prune (P08-03) → window → one summary call (P08-04) or, when the failure policy says so,
the deterministic notes → assemble head + handoff row + tail with roles alternating (calls whose
results were cut away get stub results; results whose call was cut away are dropped) → ``commit``
(the caller's write) → counters updated, anchor cleared. The attempt budget is re-armed only when
a real reading after a completed compaction is below the trigger.

Loop glue: ``run_gate(state, gate)`` and ``compress_now(state, request)`` work on a ``TurnState``:
they store pending rows, run the engine, write the result (in place: pre-compaction rows archived
as compacted through P02; rotating: a continuation published through P02), rebuild the system
prompt through ``PromptLifecycle.invalidate`` (the one sanctioned rebuild), reset the read
tracker, and emit ``Status(compressing)`` and a ``Notice`` keyed ``session.compressed``. The
runtime's ``on_compaction`` hooks run between the drop and the rebuild (a host may refresh the
stored tool array there, P13-08).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urlsplit

from indus.core.compression import texts
from indus.core.compression.anchor import (
    ImageCosts,
    MemoryBlobStore,
    SessionBlobStore,
    TokenMeter,
    estimate_messages,
)
from indus.core.compression.boundaries import (
    TailPolicy,
    find_tail_start,
    head_end,
    is_real_user,
    prune,
    window,
)
from indus.core.compression.cooldown import (
    CooldownBook,
    CounterStore,
    MemoryCounters,
    StoreCounters,
)
from indus.core.compression.fallback_summary import fallback_summary
from indus.core.compression.summarize import (
    AuxSummarizer,
    CallerSummarizer,
    Summarizer,
    SummaryFailure,
    SummaryReply,
    build_prompt,
    call_summarizer,
    handoff_text,
    previous_notes,
    records,
    sample,
    summary_budget,
)
from indus.core.compression.threshold import CompressionSettings
from indus.core.exit_reasons import ExitReason, Failure
from indus.core.repair import repair_transcript
from indus.core.transcript import TranscriptRow, call_id, call_name
from indus.protocol.events import Notice, NoticeLevel, Status, StatusKind
from indus.tools.calls import ToolResult

if TYPE_CHECKING:
    from indus.core.model_call import ModelCaller
    from indus.core.persist import TranscriptStore
    from indus.core.turn_state import TurnState
    from indus.foundation.clock import Clock
    from indus.foundation.scope import Scope

log = logging.getLogger(__name__)

__all__ = [
    "BUILTIN",
    "PROBE_AFTER_S",
    "BuiltinEngine",
    "CompressRequest",
    "ContextEngine",
    "Gate",
    "GateVerdict",
    "Outcome",
    "TokenCounts",
    "assemble",
    "build_engine",
    "caller_summarizer",
    "compress_now",
    "expect_usage",
    "run_gate",
    "start_session",
]

BUILTIN = "builtin"
PROBE_AFTER_S = 300.0
BREAKER_AT = 2
NOTICE_KEY = "session.compressed"

Gate = Literal["preflight", "pressure", "post_tool", "overflow", "manual", "idle"]


class TokenCounts(Protocol):
    """What a response's usage must tell the meter (P03 ``Usage`` and P08-08 ``CanonicalUsage``
    both do)."""

    @property
    def prompt_tokens(self) -> int: ...

    @property
    def completion_tokens(self) -> int: ...


type Commit = Callable[[list[TranscriptRow], list[TranscriptRow]], Awaitable[list[TranscriptRow]]]


@dataclass(frozen=True)
class GateVerdict:
    action: Literal["ok", "wait", "compress", "blocked"]
    tokens: int = 0
    anchored: bool = False
    reason: str = ""


@dataclass(frozen=True)
class CompressRequest:
    gate: Gate = "pressure"
    focus: str | None = None
    keep_last: int | None = None
    preview: bool = False

    @property
    def manual(self) -> bool:
        return self.gate == "manual"

    @property
    def bypass(self) -> bool:
        """A provider-proven overflow: one attempt past the cooldown and breaker."""
        return self.gate == "overflow"


@dataclass
class Outcome:
    status: Literal["compressed", "nothing", "blocked", "aborted", "preview"]
    rows: list[TranscriptRow] = field(default_factory=list[TranscriptRow])
    reason: str = ""
    failure_kind: str = ""
    fallback: bool = False
    mode: str = "in_place"
    before: int = 0
    after: int = 0
    condensed: int = 0


class ContextEngine(Protocol):
    """What the loop needs from a context engine."""

    name: str
    warned: bool
    window: int
    window_source: str
    max_output: int | None
    model: str
    provider: str

    @property
    def trigger(self) -> int: ...

    @property
    def settings(self) -> CompressionSettings: ...

    @property
    def attempts(self) -> int: ...

    async def on_session_start(self, rows: Sequence[TranscriptRow]) -> None: ...

    async def on_session_end(self) -> None: ...

    def begin_turn(self) -> None: ...

    def set_route(
        self, *, window: int, max_output: int | None, model: str, provider: str, source: str = ""
    ) -> None: ...

    async def update_from_response(
        self, usage: TokenCounts | None, priced: Sequence[TranscriptRow]
    ) -> None: ...

    def expect_usage(self, priced: Sequence[TranscriptRow] | None) -> None: ...

    async def on_usage(self, usage: TokenCounts | None, task: str) -> None: ...

    def adopt_session(self, session_id: str) -> None: ...

    async def should_compress(
        self, rows: Sequence[TranscriptRow], *, gate: Gate
    ) -> GateVerdict: ...

    async def compress(
        self, rows: Sequence[TranscriptRow], request: CompressRequest, commit: Commit
    ) -> Outcome: ...


def _messages(rows: Sequence[TranscriptRow]) -> list[dict[str, Any]]:
    return [r.to_message() for r in rows]


# -- assembling ------------------------------------------------------------------------------------


def _merge_into(row: TranscriptRow, text: str) -> TranscriptRow:
    content = row.content
    if isinstance(content, str):
        merged: Any = f"{text}\n\n{content}" if content else text
    elif isinstance(content, list):
        merged = [{"type": "text", "text": text}, *content]
    else:
        merged = text
    return row.clone(content=merged, api_content=None)


def _close_orphans(rows: list[TranscriptRow]) -> list[TranscriptRow]:
    """Stub results for calls without one; drop results whose call is gone."""
    called: dict[str, str] = {}
    for row in rows:
        if row.role == "assistant":
            for call in row.tool_calls or ():
                ident = call_id(call)
                if ident:
                    called[ident] = call_name(call) or "tool"
    answered = {r.tool_call_id for r in rows if r.role == "tool" and r.tool_call_id in called}
    out: list[TranscriptRow] = []
    index = 0
    while index < len(rows):
        row = rows[index]
        if row.role == "tool" and row.tool_call_id not in called:
            index += 1
            continue
        out.append(row)
        index += 1
        if row.role == "assistant" and row.tool_calls:
            while index < len(rows) and rows[index].role == "tool":
                if rows[index].tool_call_id in called:
                    out.append(rows[index])
                index += 1
            for call in row.tool_calls:
                ident = call_id(call)
                if ident and ident not in answered:
                    out.append(
                        TranscriptRow(
                            role="tool",
                            tool_call_id=ident,
                            name=called.get(ident),
                            content=ToolResult.error(
                                texts.PRUNED_TOOL.format(tool=called.get(ident, "tool"), chars=0),
                                error_type="compacted",
                            ).content,
                        )
                    )
                    answered.add(ident)
    return out


def _visible_role(rows: Sequence[TranscriptRow]) -> str | None:
    for row in reversed(rows):
        if row.role != "system":
            return row.role
    return None


def assemble(
    head: Sequence[TranscriptRow], text: str, tail: Sequence[TranscriptRow], *, timestamp: float
) -> list[TranscriptRow]:
    """Head + one handoff row + tail with alternating roles (see the module notes)."""
    last_head = _visible_role(head)
    role = "assistant" if last_head == "user" else "user"
    tail_rows = list(tail)
    first_tail = tail_rows[0].role if tail_rows else None
    merged = False
    if first_tail == role:
        flipped = "assistant" if role == "user" else "user"
        if last_head is not None and flipped != last_head:
            role = flipped
        elif tail_rows:
            tail_rows[0] = _merge_into(tail_rows[0], text)
            merged = True
    out = list(head)
    if not merged:
        out.append(TranscriptRow(role=role, content=text, timestamp=timestamp))
    out.extend(tail_rows)
    out = _close_orphans(out)
    repaired, _ = repair_transcript(out)
    return repaired


# -- the built-in engine ---------------------------------------------------------------------------


@dataclass
class BuiltinEngine:
    """The shipped engine (see the module notes)."""

    settings: CompressionSettings
    meter: TokenMeter
    book: CooldownBook
    clock: Clock
    summarizer: Summarizer | None = None
    window: int = 0
    window_source: str = ""
    max_output: int | None = None
    model: str = ""
    provider: str = ""
    aux_window: int | None = None
    todo: Callable[[], str | None] | None = None
    name: str = BUILTIN
    warned: bool = False
    attempts: int = 0
    compactions: int = 0
    _trigger: int = 0
    _verify: bool = False
    _compacted_this_turn: bool = False
    _started: bool = False
    _route_changed: bool = False
    _priced: list[TranscriptRow] | None = None
    _aux_checked: bool = False

    def __post_init__(self) -> None:
        self._recompute()

    # -- the trigger ------------------------------------------------------------------------------

    @property
    def trigger(self) -> int:
        return self._trigger

    def _recompute(self) -> None:
        self._trigger = self.settings.trigger(
            window=self.window,
            max_output=self.max_output,
            model=self.model,
            provider=self.provider,
            aux_window=self.aux_window,
        )

    def set_route(
        self, *, window: int, max_output: int | None, model: str, provider: str, source: str = ""
    ) -> None:
        changed = (window, model, provider) != (self.window, self.model, self.provider)
        new_runtime = (model, provider) != (self.model, self.provider)
        self.window, self.max_output, self.model, self.provider = (
            window,
            max_output,
            model,
            provider,
        )
        if source:
            self.window_source = source
        self._recompute()
        if changed:
            # Old readings cannot price another window; a new model's cooldown and strikes start
            # fresh (settled durably on the next gate).
            self._verify = False
            self.meter.anchor = None
            self.meter.last_real_prompt = 0
            self._route_changed = new_runtime

    def set_aux_window(self, aux_window: int | None) -> None:
        """A summariser with a smaller window lowers the trigger for good (warned once)."""
        if aux_window and self.window and aux_window < self.window:
            log.info(
                "the summary model's window (%d) is smaller than the main model's (%d); "
                "compression will trigger earlier",
                aux_window,
                self.window,
            )
        self.aux_window = aux_window
        self._recompute()

    def policy(self) -> TailPolicy:
        return TailPolicy(
            window=self.window,
            trigger=self._trigger,
            mode=self.settings.tail_mode,
            target_ratio=self.settings.target_ratio,
            min_user=max(1, self.settings.min_tail_user_messages),
        )

    # -- lifecycle --------------------------------------------------------------------------------

    async def on_session_start(self, rows: Sequence[TranscriptRow]) -> None:
        if self._started:
            return
        self._started = True
        await self._check_aux_window()
        await self.meter.restore(_messages(rows))

    async def _check_aux_window(self) -> None:
        """Ask the auxiliary summariser for its window once (P08-17 ``window_for``)."""
        if self._aux_checked or not isinstance(self.summarizer, AuxSummarizer):
            return
        self._aux_checked = True
        self.set_aux_window(await self.summarizer.window(self.window))

    def adopt_session(self, session_id: str) -> None:
        """A rotating compaction moved the conversation to ``session_id``: the anchor and the
        durable counters are kept on the continuation's row from now on."""
        self.meter.session_id = session_id
        counters = self.book.store
        if isinstance(counters, StoreCounters):
            counters.session_id = session_id

    async def on_session_end(self) -> None:
        self._started = False
        self.attempts = 0
        self._verify = False
        self._compacted_this_turn = False

    def begin_turn(self) -> None:
        self.attempts = 0
        self.warned = False
        self._compacted_this_turn = False

    def expect_usage(self, priced: Sequence[TranscriptRow] | None) -> None:
        """The rows the next main-loop request prices (None: no request is expected)."""
        self._priced = list(priced) if priced is not None else None

    async def on_usage(self, usage: TokenCounts | None, task: str) -> None:
        """The usage ledger's listener (P08-08): the one path a response's usage reaches the
        meter. Side tasks (``task`` set) and calls nobody announced are not the transcript's."""
        if task or self._priced is None:
            return
        priced, self._priced = self._priced, None
        await self.update_from_response(usage, priced)

    async def update_from_response(
        self, usage: TokenCounts | None, priced: Sequence[TranscriptRow]
    ) -> None:
        prompt = usage.prompt_tokens if usage is not None else 0
        completion = usage.completion_tokens if usage is not None else 0
        real = await self.meter.observe(prompt, completion, _messages(priced))
        if not real:
            return
        under = prompt < self._trigger if self._trigger else True
        if self._verify:
            self._verify = False
            counters = await self.book.read()
            count = 0 if under else counters.ineffective_count + 1
            if not under:
                log.warning(
                    "compaction did not bring the prompt under the trigger (%d ≥ %d); the system "
                    "prompt and tool list alone may be over it",
                    prompt,
                    self._trigger,
                )
            await self.book.set_ineffective(count)
        elif under:
            await self.book.set_ineffective(0)
        if under and self._compacted_this_turn:
            self.attempts = 0  # re-armed: the provider confirmed the compaction worked

    # -- gates ------------------------------------------------------------------------------------

    async def _settle_route_change(self) -> None:
        if not self._route_changed:
            return
        self._route_changed = False
        await self.book.clear()
        await self.book.set_ineffective(0)

    async def _blocked(self, request: CompressRequest | None) -> str:
        await self._settle_route_change()
        if request is not None and (request.manual or request.bypass):
            return ""
        remaining = await self.book.remaining()
        if remaining > 0:
            return f"cooldown {remaining:.0f} s"
        if self.attempts >= self.settings.max_attempts:
            return f"{self.attempts} attempts this turn"
        counters = await self.book.read()
        if counters.ineffective_count >= BREAKER_AT:
            now = self.clock.now().timestamp()
            if counters.probe_at is None or counters.probe_at - now > PROBE_AFTER_S:
                await self.book.set_probe(now + PROBE_AFTER_S)
                return "recent compactions did not help"
            if now >= counters.probe_at:
                await self.book.set_ineffective(1)  # one probe; another miss trips again
                return ""
            return "recent compactions did not help"
        return ""

    async def should_compress(self, rows: Sequence[TranscriptRow], *, gate: Gate) -> GateVerdict:
        if not self.settings.enabled or self._trigger <= 0:
            return GateVerdict("ok")
        measure = self.meter.measure(_messages(rows))
        verdict = self.meter.verdict(measure, self._trigger)
        if verdict != "compress":
            return GateVerdict(verdict, measure.tokens, measure.anchored)
        reason = await self._blocked(None)
        if reason:
            return GateVerdict("blocked", measure.tokens, measure.anchored, reason)
        return GateVerdict("compress", measure.tokens, measure.anchored)

    # -- compress ---------------------------------------------------------------------------------

    async def compress(
        self, rows: Sequence[TranscriptRow], request: CompressRequest, commit: Commit
    ) -> Outcome:
        mode = "in_place" if self.settings.in_place else "rotating"
        if request.manual and not request.preview:
            await self.book.clear()
        reason = await self._blocked(request)
        if reason:
            return Outcome("blocked", reason=reason, mode=mode)
        held = list(rows)
        before = self.meter.measure(_messages(held)).tokens
        policy = self.policy()
        head = head_end(held)
        if request.keep_last is not None:
            boundary = window(held, policy, keep_last=request.keep_last).tail_start
        else:
            boundary = find_tail_start(held, head, policy)
        pruned = prune(held, boundary)
        cut = window(pruned.rows, policy, keep_last=request.keep_last)
        middle = [r for r in pruned.rows[cut.head_end : cut.tail_start] if not r.scaffold]
        if cut.empty or not records(middle):
            return Outcome("nothing", reason=texts.NOTHING_TO_COMPRESS, mode=mode, before=before)
        if request.preview:
            return Outcome(
                "preview",
                reason=f"{len(middle)} message(s) would be condensed",
                mode=mode,
                before=before,
                condensed=len(middle),
            )
        if not request.manual:
            self.attempts += 1
        previous = previous_notes(pruned.rows[: cut.tail_start])
        budget = summary_budget(middle, self.window)
        text_in, _ = sample(records(middle))
        prompt = build_prompt(
            text_in,
            budget=budget,
            previous=previous,
            focus=request.focus,
            mode=self.settings.tail_mode,
        )
        fallback = False
        failure_kind = ""
        try:
            notes = await call_summarizer(
                self.summarizer, prompt, timeout_s=self.settings.timeout_s, clock=self.clock
            )
        except SummaryFailure as failed:
            failure_kind = failed.kind
            decision = await self.book.failure(failed.kind, failed.detail)
            if not decision.commit_fallback:
                return Outcome(
                    "aborted",
                    reason=failed.detail,
                    failure_kind=failed.kind,
                    mode=mode,
                    before=before,
                )
            notes = fallback_summary(middle, previous=previous, reason=failed.detail)
            fallback = True
        todo = self.todo() if self.todo is not None else None
        text = handoff_text(
            notes,
            middle,
            skills=pruned.skills,
            extras=fallback or self.settings.tail_mode == "lean",
            todo=todo,
        )
        assembled = assemble(
            pruned.rows[: cut.head_end],
            text,
            pruned.rows[cut.tail_start :],
            timestamp=self.clock.now().timestamp(),
        )
        try:
            stored = await commit(assembled, held)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("could not store the compacted transcript: %s", exc)
            if not request.manual:
                self.attempts = max(0, self.attempts - 1)
            return Outcome(
                "aborted", reason=str(exc), failure_kind="store", mode=mode, before=before
            )
        await self.book.success(fallback=fallback)
        await self.meter.clear(after_compaction=True)
        self._verify = True
        self._compacted_this_turn = True
        self.compactions += 1
        after = estimate_messages(_messages(stored), self.meter.image_cost)
        log.info(
            "compressed %d message(s) (%s, %s): ~%d → ~%d tokens",
            len(middle),
            mode,
            "deterministic notes" if fallback else "model summary",
            before,
            after,
        )
        return Outcome(
            "compressed",
            rows=stored,
            fallback=fallback,
            failure_kind=failure_kind,
            mode=mode,
            before=before,
            after=after,
            condensed=len(middle),
        )


# -- building --------------------------------------------------------------------------------------


def caller_summarizer(caller: ModelCaller) -> CallerSummarizer:
    """A summariser that asks the main route (``auto``) through the session's ``ModelCaller``."""
    from indus.providers.streaming import CallResult
    from indus.providers.types import ChatRequest

    async def chat(messages: list[dict[str, Any]]) -> SummaryReply:
        request = ChatRequest(model=caller.model, messages=messages, temperature=0.2)
        outcome = await caller.call(request, on_event=lambda _event: None)
        if not isinstance(outcome, CallResult):
            raise SummaryFailure("error", "the route changed during the summary call")
        response = outcome.response
        return SummaryReply(response.content or "", response.finish_reason)

    return CallerSummarizer(chat)


def _host(base_url: str) -> str:
    try:
        return (urlsplit(base_url).hostname or "").lower()
    except ValueError:
        return ""


def build_engine(
    scope: Scope | None,
    *,
    session_id: str,
    clock: Clock,
    store: TranscriptStore | None = None,
    summarizer: Summarizer | None = None,
    window: int = 0,
    window_source: str = "",
    max_output: int | None = None,
    model: str = "",
    provider: str = "",
    base_url: str = "",
    images: ImageCosts | None = None,
    todo: Callable[[], str | None] | None = None,
    settings: CompressionSettings | None = None,
) -> BuiltinEngine | None:
    """The session's engine, or None when compression is off."""
    from indus.core.persist import StateStore

    chosen = settings or CompressionSettings.from_scope(scope)
    if not chosen.enabled:
        return None
    if chosen.engine != BUILTIN:
        log.warning(
            "context engine %r is not available (plugin engines arrive with P13); using the "
            "built-in one",
            chosen.engine,
        )
    if isinstance(store, StateStore):
        blobs: Any = SessionBlobStore(store.handle)
        counters: CounterStore = StoreCounters(store.handle, session_id, blobs)
    else:
        blobs = MemoryBlobStore()
        counters = MemoryCounters()
    meter = TokenMeter(
        session_id=session_id,
        store=blobs,
        images=images or ImageCosts(),
        image_key=ImageCosts.key(model, _host(base_url)),
    )
    book = CooldownBook(
        counters,
        clock,
        timeout_s=chosen.timeout_s,
        abort_on_failure=chosen.abort_on_summary_failure,
    )
    engine = BuiltinEngine(
        settings=chosen,
        meter=meter,
        book=book,
        clock=clock,
        summarizer=summarizer if summarizer is not None else AuxSummarizer(),
        window=window,
        window_source=window_source,
        max_output=max_output,
        model=model,
        provider=provider,
        todo=todo,
    )
    return engine


# -- loop glue -------------------------------------------------------------------------------------


def _engine(state: TurnState) -> ContextEngine | None:
    engine = getattr(state.runtime, "context", None)
    return engine if engine is not None else None


def durable(state: TurnState) -> list[TranscriptRow]:
    return [r for r in state.rows if not r.scaffold]


async def start_session(state: TurnState) -> None:
    """Let the engine adopt its persisted state for the loaded history (once per process)."""
    engine = _engine(state)
    if engine is None:
        return
    engine.begin_turn()
    try:
        await engine.on_session_start(durable(state))
    except Exception:
        log.warning("context engine could not start for %s", state.session_id, exc_info=True)


def expect_usage(state: TurnState, *, done: bool = False) -> None:
    """Announce (or, with ``done``, withdraw) the rows the next main-loop request prices; the
    engine reads the response's usage from the ledger's listener (``on_usage``)."""
    engine = _engine(state)
    if engine is not None:
        engine.expect_usage(None if done else durable(state))


async def _commit_rows(
    state: TurnState, rows: list[TranscriptRow], held: list[TranscriptRow], *, in_place: bool
) -> list[TranscriptRow]:
    from indus.core.persist import StateStore

    store = state.runtime.store
    fresh = [r.clone(row_id=None) for r in rows]
    if store is None:
        return fresh
    held_ids = [r.row_id for r in held]
    if any(i is None or i < 0 for i in held_ids):
        raise RuntimeError("the transcript is not fully stored; compression waits")
    ids = [int(i) for i in held_ids if i is not None]
    if isinstance(store, StateStore):
        from indus.state import compression

        handle = store.handle
        session_id = state.session_id
        data = [r.to_store() for r in fresh]
        now = state.clock.now().timestamp()
        if in_place:
            new_ids = await handle.awrite(
                lambda conn: compression.compact_in_place_in(conn, session_id, data, ids, now=now)
            )
        else:
            new_ids = await _rotate(state, data, ids)
    else:
        new_ids = await store.replace(state.session_id, fresh, expect_ids=ids)
    for row, ident in zip(fresh, new_ids, strict=False):
        row.row_id = ident
    return fresh


async def _rotate(state: TurnState, data: list[dict[str, Any]], held_ids: list[int]) -> list[int]:
    """Publish a continuation session (P02) and move the runtime onto it."""
    from indus.core.persist import StateStore
    from indus.foundation.blocking import run_blocking
    from indus.state import compression, messages

    store = state.runtime.store
    assert isinstance(store, StateStore)
    handle = store.handle
    parent = state.session_id
    holder = f"compress:{parent}:{state.turn_id}"

    def publish() -> tuple[str, list[int]]:
        if not compression.acquire(handle, parent, holder):
            raise compression.CompressionBusy(f"session {parent} is already being compressed")
        try:
            mark = max(held_ids) if held_ids else 0
            child = compression.publish_continuation(handle, parent, {}, data, mark, holder)
        finally:
            compression.release(handle, parent, holder)
        live = [int(m["id"]) for m in messages.get(handle, child)]
        return child, live

    child, live = await run_blocking(
        publish, pool="io", deadline=60.0, label="publish continuation"
    )
    state.runtime.session_id = child
    state.runtime.prompt_context = replace(state.runtime.prompt_context, session_id=child)
    log.info("session %s continues in %s after compression", parent, child)
    engine = _engine(state)
    if engine is not None:
        engine.adopt_session(child)
    moved = state.runtime.on_continued
    if moved is not None:
        try:
            moved(parent, child)
        except Exception:
            log.warning("the host could not follow %s to %s", parent, child, exc_info=True)
    return live


def _last_user(rows: Sequence[TranscriptRow]) -> int:
    for index in range(len(rows) - 1, -1, -1):
        if is_real_user(rows[index]) and not rows[index].is_steer:
            return index
    return -1


async def _apply(
    state: TurnState, rows: list[TranscriptRow], user_index: int, scaffolds: list[TranscriptRow]
) -> None:
    state.rows = rows + scaffolds
    state.user_index = user_index if user_index >= 0 else _last_user(rows)
    state.stored_at_start = 0
    runtime = state.runtime
    runtime.prompts.invalidate("compaction")
    for hook in list(runtime.on_compaction):
        try:
            hook()
        except Exception:
            log.warning("a compaction hook of %s failed", state.session_id, exc_info=True)
    ctx = replace(runtime.prompt_context, session_id=state.session_id)
    state.system_prompt = await runtime.prompts.restore_or_build(
        ctx, history=[r.to_message() for r in rows]
    )
    try:
        from indus.tools.builtin import read_tracking

        read_tracking.compaction_boundary(runtime.scope, state.session_id)
    except Exception:
        log.debug("read tracker reset failed", exc_info=True)


async def compress_now(state: TurnState, request: CompressRequest) -> Outcome:
    """Run one compression on the turn's transcript and carry the result into the turn."""
    engine = _engine(state)
    if engine is None:
        return Outcome("nothing", reason="compression is off")
    await state.flush()
    scaffolds = [r for r in state.rows if r.scaffold]
    current = state.rows[state.user_index] if 0 <= state.user_index < len(state.rows) else None
    rows = durable(state)
    in_place = engine.settings.in_place
    located: list[int] = []

    async def commit(new: list[TranscriptRow], held: list[TranscriptRow]) -> list[TranscriptRow]:
        located[:] = [i for i, r in enumerate(new) if current is not None and r is current]
        return await _commit_rows(state, new, held, in_place=in_place)

    if not request.preview:
        state.emit(Status, kind=StatusKind.COMPRESSING, text=texts.STATUS_RUNNING)
    try:
        outcome = await engine.compress(rows, request, commit)
    finally:
        if not request.preview:
            state.emit(Status, kind=StatusKind.WORKING)
    if outcome.status == "compressed":
        await _apply(state, outcome.rows, located[0] if located else -1, scaffolds)
        if outcome.fallback:
            text = texts.NOTICE_FALLBACK.format(reason=outcome.failure_kind or "unavailable")
        else:
            text = texts.NOTICE_DONE.format(
                before=outcome.before, after=outcome.after, mode=outcome.mode
            )
        state.emit(Notice, level=NoticeLevel.INFO, text=text, key=NOTICE_KEY)
    elif outcome.status == "aborted":
        state.emit(
            Notice,
            level=NoticeLevel.WARNING,
            text=texts.NOTICE_FAILED.format(reason=outcome.failure_kind or outcome.reason),
            key=NOTICE_KEY,
        )
    return outcome


async def run_gate(state: TurnState, gate: Gate) -> Outcome | None:
    """An automatic gate: compress when the engine says so. None when nothing was attempted."""
    engine = _engine(state)
    if engine is None or state.ended:
        return None
    verdict = await engine.should_compress(durable(state), gate=gate)
    if verdict.action == "blocked":
        if not engine.warned:
            engine.warned = True
            state.emit(
                Notice,
                level=NoticeLevel.WARNING,
                text=texts.NOTICE_BLOCKED.format(reason=verdict.reason),
                key="compression.blocked",
            )
        return None
    if verdict.action != "compress":
        return None
    outcome = await compress_now(state, CompressRequest(gate=gate))
    if (
        gate == "preflight"
        and outcome.status == "aborted"
        and outcome.failure_kind == "stall"
        and engine.window > 0
        and verdict.tokens >= engine.window
    ):
        state.end(
            ExitReason.COMPRESSION_EXHAUSTED,
            final_text=texts.EXHAUSTED_REPLY,
            failure=Failure("compression_timeout", True, detail="the summary call timed out"),
        )
    return outcome
