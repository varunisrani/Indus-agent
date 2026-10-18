"""The summary call (P08-04): records, budget, prompt, one auxiliary call, the handoff text.

- ``records(rows)`` serialises the middle turns (role, text, tool calls with arguments cut to
  1,500 characters, tool results cut head 4,000 + tail 1,500), redacted;
- ``sample(records)`` keeps the summariser input within 160,000 characters by keeping 8 evenly
  spaced slices of whole records (the newest record always), with markers for what was left out;
- ``summary_budget(rows, window)`` = ``tokens × 0.20`` clamped to
  ``[2,000, min(window × 0.05, SUMMARY_CEILING)]``. The ceiling is the code constant 10,000 until
  REA answers 01 Q6 (D-16.24). The budget is guidance inside the prompt; the call itself is not
  capped on the wire, so a summary cut by the provider's own output cap shows up as
  ``finish_reason == "length"`` (failure kind ``truncated``);
- ``build_prompt`` writes our prompt: fresh, or an update of the previous notes; a focus topic
  goes last so it wins;
- ``call_summarizer`` makes the one call under the engine's deadline and turns every failure into
  a ``SummaryFailure`` with a kind the cooldown ladder understands;
- ``handoff_text`` wraps the notes in the open/close markers and appends the deterministic parts
  (skills to reload; in lean mode or for the fallback: exact identifiers, the user's messages
  verbatim, the pointer to session search; the carried-over task list).

``Summarizer`` is the seam to the auxiliary client (P08-17). ``AuxSummarizer`` calls
``AuxClient.call("compression", …)`` and reads ``window_for("compression")``; without a client it
uses the main route's caller (``auto`` means "the main route and model").
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from indus.core.compression import texts
from indus.core.compression.anchor import estimate_messages
from indus.core.compression.fallback_summary import anchor_index, row_text, user_messages
from indus.core.transcript import TranscriptRow, call_arguments, call_name
from indus.foundation.errors import ProviderHTTPError, ProviderStreamError
from indus.foundation.redact import redact_text

if TYPE_CHECKING:
    from indus.foundation.clock import Clock

log = logging.getLogger(__name__)

__all__ = [
    "INPUT_MAX_CHARS",
    "SUMMARY_CEILING",
    "SUMMARY_FLOOR",
    "AuxCaller",
    "AuxSummarizer",
    "CallerSummarizer",
    "FailureKind",
    "Summarizer",
    "SummaryFailure",
    "SummaryReply",
    "build_prompt",
    "call_summarizer",
    "classify_failure",
    "handoff_text",
    "previous_notes",
    "records",
    "sample",
    "summary_budget",
]

SUMMARY_FLOOR = 2_000
SUMMARY_RATIO = 0.20
SUMMARY_CEILING = 10_000  # D-16.24: the code constant until REA answers 01 Q6
SUMMARY_WINDOW_SHARE = 0.05
LEAN_LOG_TOKENS = 4_000
INPUT_MAX_CHARS = 160_000
SLICES = 8
RECORD_MAX = 6_000
RECORD_HEAD = 4_000
RECORD_TAIL = 1_500
ARGS_MAX = 1_500
SEPARATOR = "\n\n"

FailureKind = Literal[
    "stall", "truncated", "decode", "empty", "overload", "access", "unavailable", "error"
]

_REFUSALS = ("i can't help", "i cannot help", "i'm sorry, but i can", "i am unable to")


@dataclass(frozen=True)
class SummaryReply:
    text: str
    finish_reason: str | None = "stop"


class SummaryFailure(Exception):
    """The summary call failed; ``kind`` picks the cooldown rung."""

    def __init__(self, kind: FailureKind, detail: str = "") -> None:
        super().__init__(detail or kind)
        self.kind: FailureKind = kind
        self.detail = detail or kind


class Summarizer(Protocol):
    async def summarize(self, prompt: str) -> SummaryReply: ...


# -- records ------------------------------------------------------------------------------------


def _elide_middle(text: str, head: int, tail: int) -> str:
    if len(text) <= head + tail:
        return text
    left = len(text) - head - tail
    return text[:head] + "\n" + texts.RECORD_ELIDED.format(chars=left) + "\n" + text[-tail:]


def _record(row: TranscriptRow) -> str:
    if row.role == "tool":
        body = row.content if isinstance(row.content, str) else row_text(row)
        body = redact_text(body)
        if len(body) > RECORD_MAX:
            body = _elide_middle(body, RECORD_HEAD, RECORD_TAIL)
        return f"[tool result: {row.name or 'tool'}]\n{body}"
    text = row.content if isinstance(row.content, str) else row_text(row)
    text = redact_text(text or "")
    if len(text) > RECORD_MAX:
        text = _elide_middle(text, RECORD_HEAD, RECORD_TAIL)
    lines = [f"[{row.role}]", text] if text else [f"[{row.role}]"]
    for call in row.tool_calls or ():
        args = redact_text(call_arguments(call) or "")
        if len(args) > ARGS_MAX:
            args = args[:ARGS_MAX] + "…"
        lines.append(f"  -> {call_name(call) or 'tool'}({args})")
    return "\n".join(lines)


def records(rows: Sequence[TranscriptRow]) -> list[str]:
    """The middle turns as text records (system rows and earlier notes left out)."""
    return [_record(r) for r in rows if r.role != "system" and not _is_handoff(r)]


def _bound(record: str, limit: int) -> str:
    if len(record) <= limit:
        return record
    marker = texts.RECORD_ELIDED.format(chars=len(record))
    room = max(0, limit - len(marker) - 2)
    head = room // 2
    return (
        record[:head]
        + "\n"
        + texts.RECORD_ELIDED.format(chars=len(record) - room)
        + "\n"
        + record[len(record) - (room - head) :]
    )


def sample(items: Sequence[str], limit: int = INPUT_MAX_CHARS) -> tuple[str, bool]:
    """``items`` joined, or evenly sampled slices of whole records within ``limit``."""
    if not items:
        return "", False
    total = sum(len(r) for r in items) + len(SEPARATOR) * (len(items) - 1)
    if total <= limit:
        return SEPARATOR.join(items), False
    count = len(items)
    slices = max(1, min(SLICES, count))
    marker_room = len(texts.INPUT_ELIDED.format(first=count, last=count, chars=total)) + 8
    target = max(1, (limit - marker_room * slices) // slices)
    shown = [_bound(r, target) for r in items]
    chosen: list[tuple[int, int]] = []
    for index in range(slices):
        if index == slices - 1:
            end = count
            start = end - 1
            size = len(shown[start])
            while start > 0 and size + len(SEPARATOR) + len(shown[start - 1]) <= target:
                start -= 1
                size += len(SEPARATOR) + len(shown[start])
        else:
            start = round(index * count / slices)
            end = start
            size = 0
            while end < count and size + len(shown[end]) <= target:
                size += len(shown[end]) + len(SEPARATOR)
                end += 1
            end = max(end, start + 1)
        if chosen and start <= chosen[-1][1]:
            chosen[-1] = (chosen[-1][0], max(chosen[-1][1], end))
        else:
            chosen.append((start, end))
    pieces: list[str] = []
    cursor = 0
    for start, end in chosen:
        if start > cursor:
            left_out = sum(len(r) for r in items[cursor:start])
            pieces.append(texts.INPUT_ELIDED.format(first=cursor + 1, last=start, chars=left_out))
        pieces.extend(shown[start:end])
        cursor = end
    text = SEPARATOR.join(pieces)
    if len(text) > limit:
        keep_head = int(limit * 0.45)
        keep_tail = limit - keep_head - 80
        text = _elide_middle(text, keep_head, max(0, keep_tail))[:limit]
    return text, True


def summary_budget(rows: Sequence[TranscriptRow], window: int) -> int:
    """Output guidance for the notes in tokens (see the module notes)."""
    content = estimate_messages([r.to_message() for r in rows])
    ceiling = (
        min(int(window * SUMMARY_WINDOW_SHARE), SUMMARY_CEILING) if window > 0 else SUMMARY_CEILING
    )
    return max(SUMMARY_FLOOR, min(int(content * SUMMARY_RATIO), ceiling))


# -- the prompt ---------------------------------------------------------------------------------


def build_prompt(
    records_text: str,
    *,
    budget: int,
    previous: str | None = None,
    focus: str | None = None,
    mode: str = "lean",
) -> str:
    sections = texts.SECTIONS.format(budget=budget)
    if mode == "lean":
        sections += texts.LEAN_LOG_SECTION.format(budget=LEAN_LOG_TOKENS)
    parts = [texts.SUMMARIZER_RULES, ""]
    if previous and previous.strip():
        bounded, _ = sample([previous.strip()])
        parts += [
            texts.UPDATE_TASK,
            "",
            texts.PREVIOUS_OPEN,
            bounded,
            texts.PREVIOUS_CLOSE,
            "",
        ]
    else:
        parts += [texts.FRESH_TASK, ""]
    parts += [texts.RECORDS_OPEN, records_text, texts.RECORDS_CLOSE, "", sections]
    prompt = "\n".join(parts)
    if focus and focus.strip():
        prompt += texts.FOCUS_BLOCK.format(focus=focus.strip())
    return prompt


# -- handoff rows ---------------------------------------------------------------------------------


def _is_handoff(row: TranscriptRow) -> bool:
    return isinstance(row.content, str) and row.content.startswith(texts.SUMMARY_OPEN)


def previous_notes(rows: Sequence[TranscriptRow]) -> str | None:
    """The body of the newest handoff row in ``rows`` (our open/close markers removed)."""
    for row in reversed(rows):
        if _is_handoff(row):
            body = cast(str, row.content)[len(texts.SUMMARY_OPEN) :]
            cut = body.find(texts.SUMMARY_CLOSE)
            if cut >= 0:
                body = body[:cut]
            return body.strip() or None
    return None


def handoff_text(
    notes: str,
    middle: Sequence[TranscriptRow],
    *,
    skills: Sequence[str] = (),
    extras: bool = True,
    todo: str | None = None,
) -> str:
    """The handoff row's text: markers + notes + the deterministic sections."""
    blocks = [texts.SUMMARY_OPEN, notes.strip()]
    if skills:
        markers = "\n".join(texts.SKILL_MARKER.format(name=n) for n in skills)
        blocks.append(f"{texts.SKILLS_HEADING}\n{texts.SKILLS_NOTE}\n{markers}")
    if extras:
        index = anchor_index(middle)
        if index:
            blocks.append(f"{texts.ANCHOR_HEADING}\n{index}\n{texts.ANCHOR_NOTE}")
        said = user_messages(middle)
        if said:
            quoted = "\n\n".join(f"{n}. {text}" for n, text in enumerate(said, 1))
            blocks.append(f"{texts.USERS_HEADING}\n{quoted}")
        counted = sum(1 for r in middle if r.role != "system")
        blocks.append(f"{texts.RECOVERY_HEADING}\n{texts.RECOVERY_NOTE.format(count=counted)}")
    if todo:
        blocks.append(todo)
    blocks.append(texts.SUMMARY_CLOSE)
    return "\n\n".join(b for b in blocks if b)


# -- the call -----------------------------------------------------------------------------------


def classify_failure(exc: BaseException) -> FailureKind:
    """The cooldown class of a failed summary call."""
    if isinstance(exc, SummaryFailure):
        return exc.kind
    if isinstance(exc, TimeoutError):
        return "stall"
    code = str(getattr(exc, "code", "") or "")
    if code == "provider.aux_timeout":  # P08-17 ``AuxTimeout``
        return "stall"
    if code == "provider.aux_unavailable":  # P08-17 ``AuxUnavailable``: no route could answer
        return "unavailable"
    classified = getattr(exc, "classified", None)
    reason = str(getattr(classified, "reason", "") or "")
    if reason:
        if reason == "timeout":
            return "stall"
        if reason in ("overloaded", "rate_limit", "upstream_rate_limit", "server_error"):
            return "overload"
        if reason in ("auth", "auth_permanent", "billing"):
            return "access"
        if reason == "connection":
            return "decode"
        return "error"
    if isinstance(exc, ProviderHTTPError):
        status = getattr(exc, "status", 0)
        if status in (429, 503, 529):
            return "overload"
        if status in (401, 402, 403):
            return "access"
        return "error"
    if isinstance(exc, (ProviderStreamError, json.JSONDecodeError, ConnectionError)):
        return "decode"
    text = str(exc).lower()
    if "overloaded" in text or "at capacity" in text:
        return "overload"
    return "error"


async def _deadline[T](work: Awaitable[T], seconds: float, clock: Clock | None) -> T:
    if clock is None:
        return await asyncio.wait_for(work, seconds)
    task = asyncio.ensure_future(work)
    timer = asyncio.ensure_future(clock.sleep(seconds))
    try:
        done, _ = await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return task.result()
        raise TimeoutError(f"the summary call took longer than {seconds:.0f} s")
    finally:
        timer.cancel()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def call_summarizer(
    summarizer: Summarizer | None,
    prompt: str,
    *,
    timeout_s: float,
    clock: Clock | None = None,
) -> str:
    """Run the one summary call; the notes text, or ``SummaryFailure`` with its kind."""
    if summarizer is None:
        raise SummaryFailure("unavailable", "no auxiliary model is configured")
    try:
        reply = await _deadline(summarizer.summarize(prompt), timeout_s, clock)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        kind = classify_failure(exc)
        raise SummaryFailure(kind, str(exc) or kind) from exc
    if reply.finish_reason == "length":
        raise SummaryFailure("truncated", "the summary hit the output cap")
    text = (reply.text or "").strip()
    if not text or text.lower().startswith(_REFUSALS):
        raise SummaryFailure("empty", "the summary call returned no notes")
    return redact_text(text)


# -- adapters ------------------------------------------------------------------------------------

type ChatCall = Callable[[list[dict[str, Any]]], Awaitable[SummaryReply]]


class CallerSummarizer:
    """Summaries through a plain chat call (used for the main route when no aux client exists)."""

    def __init__(self, chat: ChatCall) -> None:
        self.chat = chat

    async def summarize(self, prompt: str) -> SummaryReply:
        return await self.chat([{"role": "user", "content": prompt}])


class AuxCaller(Protocol):
    """What the P08-17 auxiliary client offers (``indus.providers.auxiliary.AuxClient``): ``call``
    answers with an ``AuxResult`` (``text`` plus the ``response`` and its ``finish_reason``);
    ``window_for`` is the task route's window, given the main model's."""

    async def call(self, task: str, messages: Sequence[Any]) -> Any: ...

    async def window_for(self, task: str, main_window: int) -> int: ...


class AuxSummarizer:
    """``client.call("compression", …)`` on the auxiliary client (P08-17); ``fallback`` (the main
    route) when no client is wired."""

    TASK = "compression"

    def __init__(self, client: AuxCaller | None = None, fallback: Summarizer | None = None) -> None:
        self.client = client
        self.fallback = fallback

    async def window(self, main_window: int) -> int | None:
        """The summariser's context window, when the client knows it (None without a client)."""
        if self.client is None or main_window <= 0:
            return None
        try:
            return await self.client.window_for(self.TASK, main_window)
        except Exception:
            log.debug("auxiliary window lookup failed", exc_info=True)
            return None

    async def summarize(self, prompt: str) -> SummaryReply:
        if self.client is None:
            if self.fallback is None:
                raise SummaryFailure("unavailable", "no auxiliary model is configured")
            return await self.fallback.summarize(prompt)
        result = await self.client.call(self.TASK, [{"role": "user", "content": prompt}])
        if isinstance(result, str):
            return SummaryReply(result, "stop")
        text = getattr(result, "text", None)
        response = getattr(result, "response", None)
        finish = getattr(response, "finish_reason", None) or getattr(result, "finish_reason", None)
        return SummaryReply(str(text or ""), str(finish) if finish else "stop")
