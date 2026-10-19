"""Send results, the send-error classifier and the inline retry policy (P15-04).

Every outbound platform call answers with a `SendResult`. Failures are sorted into seven kinds
by an ordered rule table (first match wins; nothing matched is ``unknown``, never harmless).

Retry rules (R06.3, R06.4):
- read/write timeouts are never retried: the platform may already have the message;
- connection-level failures and explicit rate limits are retried, at most ``max_retries`` times,
  with backoff ``base_delay * 2**n`` plus jitter; a server ``retry_after`` replaces one backoff;
- a server wait above the inline cap is not slept: the typed flood failure is returned so the
  delivery ledger owns the wait, and no "delivery failed" notice is sent into the penalty;
- a formatting failure is re-sent once as plain text;
- a split payload that partly landed is never sent whole again: only a remainder the adapter
  proves undelivered is sent.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum

from indus.foundation.clock import Clock

log = logging.getLogger(__name__)

DELIVERY_FAILED_NOTICE = (
    "Sorry, I could not deliver my last reply because of a connection problem. "
    "Ask again if you still need it."
)


class ErrorKind(StrEnum):
    TOO_LONG = "too_long"
    BAD_FORMAT = "bad_format"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    TRANSIENT = "transient"
    UNKNOWN = "unknown"


# -- classifier -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Rule:
    kind: ErrorKind
    needles: tuple[str, ...]
    classes: tuple[type[BaseException], ...] = ()
    # Matches only when every needle of one group is present (``("parse", "entit")``).
    together: tuple[tuple[str, ...], ...] = ()

    def matches(self, blob: str, exc: BaseException | None) -> bool:
        if any(needle in blob for needle in self.needles):
            return True
        if any(all(n in blob for n in group) for group in self.together):
            return True
        return exc is not None and isinstance(exc, self.classes)


# A whole chat is gone.
_CHAT_GONE = (
    "chat not found",
    "chat_not_found",
    "channel not found",
    "channel_not_found",
    "unknown channel",
    "peer_id_invalid",
)
# Something inside a chat is gone; the chat itself is still reachable.
_SUB_CHAT_GONE = (
    "thread not found",
    "thread_not_found",
    "topic not found",
    "topic_deleted",
    "topic_closed",
    "message to edit not found",
    "message to reply not found",
    "message to delete not found",
    "message_id_invalid",
    "message_not_found",
    "message not found",
    "unknown message",
)
# The request may have reached the platform before the wait ran out.
_AMBIGUOUS = (
    "timed out",
    "timedout",
    "readtimeout",
    "writetimeout",
    "read timeout",
    "write timeout",
    "readerror",
    "timeouterror",
    "response ended prematurely",
)
# The request never reached the platform.
_CONNECT_LEVEL = (
    "connecterror",
    "connectionerror",
    "connect error",
    "connecttimeout",
    "connect timeout",
    "pooltimeout",
    "connection refused",
    "connectionrefused",
    "connection reset",
    "connectionreset",
    "connection aborted",
    "connectionaborted",
    "broken pipe",
    "brokenpipe",
    "remotedisconnected",
    "server disconnected",
    "cannot connect to host",
    "network is unreachable",
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname",
    "eoferror",
    "networkerror",
)

_RULES: tuple[_Rule, ...] = (
    _Rule(
        ErrorKind.TOO_LONG,
        ("too long", "too_long", "msg_too_long", "or fewer in length", "message is too big"),
    ),
    _Rule(
        ErrorKind.BAD_FORMAT,
        (
            "can't parse",
            "cant parse",
            "can not parse",
            "unsupported start tag",
            "can't find end",
            "invalid_blocks",
            "invalid_block",
            "invalid_mrkdwn",
            "invalid form body",
        ),
        together=(("parse", "entit"), ("bad request", "entit")),
    ),
    _Rule(
        ErrorKind.FORBIDDEN,
        (
            "forbidden",
            "bot was blocked",
            "blocked by the user",
            "user is deactivated",
            "bot was kicked",
            "not enough rights",
            "have no rights",
            "not a member",
            "not_in_channel",
            "is_archived",
            "missing access",
            "missing permissions",
            "cannot send messages to this user",
            "access_denied",
        ),
        (PermissionError,),
    ),
    _Rule(ErrorKind.NOT_FOUND, (*_CHAT_GONE, *_SUB_CHAT_GONE)),
    _Rule(
        ErrorKind.RATE_LIMITED,
        (
            "flood",
            "too many requests",
            "retry after",
            "retryafter",
            "rate limit",
            "ratelimit",
            "rate_limit",
            "slow down",
        ),
    ),
    # Ambiguous timeouts sort before the connection rule: a wrapper such as
    # "NetworkError: ReadTimeout" must never be retried.
    _Rule(ErrorKind.UNKNOWN, _AMBIGUOUS, (TimeoutError,)),
    _Rule(
        ErrorKind.TRANSIENT,
        _CONNECT_LEVEL,
        (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError),
    ),
)


def _blob(exc: BaseException | None, text: str) -> str:
    parts = [text] if text else []
    if exc is not None:
        parts.extend(p for p in (str(exc), type(exc).__name__) if p)
    return " ".join(parts).lower()


def classify_send_error(exc: BaseException | None = None, text: str = "") -> ErrorKind:
    """The kind of a send failure; ``unknown`` when no rule matches."""
    blob = _blob(exc, text)
    for rule in _RULES:
        if rule.matches(blob, exc):
            return rule.kind
    return ErrorKind.UNKNOWN


def is_chat_level_not_found(exc: BaseException | None = None, text: str = "") -> bool:
    """True when "not found" means the whole chat; a sub-chat reading wins when both appear."""
    blob = _blob(exc, text)
    if any(needle in blob for needle in _SUB_CHAT_GONE):
        return False
    return any(needle in blob for needle in _CHAT_GONE)


def is_ambiguous_timeout(exc: BaseException | None = None, text: str = "") -> bool:
    """True for read/write timeouts: the platform may have the message, so never re-send."""
    blob = _blob(exc, text)
    if any(needle in blob for needle in _AMBIGUOUS):
        return True
    return exc is not None and isinstance(exc, TimeoutError)


# -- result -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SendResult:
    ok: bool
    message_id: str | None = None
    error: str | None = None
    retryable: bool = False
    retry_after: float | None = None
    # Earlier chunk ids in send order when a payload was split; ``message_id`` is the last one.
    continuation_ids: tuple[str, ...] = ()
    kind: ErrorKind | None = None
    # ``not_found`` that means the whole chat.
    chat_level: bool = False
    # A timeout: the platform may have the message.
    maybe_delivered: bool = False
    # Part of a split payload is already visible.
    partial: bool = False
    # Text the adapter proves was not delivered (only with ``partial``); None = cannot resume.
    remainder: str | None = None
    # An optional operation this platform does not have; nothing was sent.
    unsupported: bool = False
    # An interim edit dropped because the chat's send slot was busy (``ok`` stays true).
    skipped: bool = False
    extra: dict[str, object] = field(default_factory=dict[str, object], compare=False)

    @classmethod
    def sent(cls, message_id: str, continuation_ids: tuple[str, ...] = ()) -> SendResult:
        return cls(ok=True, message_id=str(message_id), continuation_ids=tuple(continuation_ids))

    @classmethod
    def failure(
        cls,
        error: str | None = None,
        *,
        exc: BaseException | None = None,
        retry_after: float | None = None,
        partial: bool = False,
        remainder: str | None = None,
        delivered_ids: tuple[str, ...] = (),
    ) -> SendResult:
        """A classified failure. ``delivered_ids`` are the chunks that did land (``partial``)."""
        text = error if error is not None else (f"{type(exc).__name__}: {exc}" if exc else "")
        kind = classify_send_error(exc, text)
        if retry_after is not None and kind is not ErrorKind.RATE_LIMITED:
            kind = ErrorKind.RATE_LIMITED
        ambiguous = kind is ErrorKind.UNKNOWN and is_ambiguous_timeout(exc, text)
        ids = tuple(delivered_ids)
        return cls(
            ok=False,
            message_id=ids[-1] if ids else None,
            continuation_ids=ids[:-1],
            error=text or "send failed",
            retryable=kind in (ErrorKind.TRANSIENT, ErrorKind.RATE_LIMITED),
            retry_after=retry_after,
            kind=kind,
            chat_level=kind is ErrorKind.NOT_FOUND and is_chat_level_not_found(exc, text),
            maybe_delivered=ambiguous,
            partial=partial or bool(ids),
            remainder=remainder if (partial or ids) else None,
        )

    @classmethod
    def refused(cls, what: str) -> SendResult:
        return cls(ok=False, error=f"{what} is not supported on this platform", unsupported=True)

    @classmethod
    def skip(cls) -> SendResult:
        """An interim edit dropped because the chat's send slot was busy: not a failure (the
        next frame carries the text), so ``ok`` with ``skipped`` set."""
        return cls(ok=True, skipped=True)

    @property
    def ids(self) -> tuple[str, ...]:
        """Every delivered id in send order."""
        tail = (self.message_id,) if self.message_id is not None else ()
        return (*self.continuation_ids, *tail)

    @property
    def chat_unreachable(self) -> bool:
        """The whole chat cannot be reached (dead-target registry, P15-17)."""
        if self.ok:
            return False
        return self.kind is ErrorKind.FORBIDDEN or (
            self.kind is ErrorKind.NOT_FOUND and self.chat_level
        )

    @property
    def is_flood(self) -> bool:
        """Rate limited with a server wait: the delivery ledger retries after ``retry_after``."""
        return not self.ok and self.kind is ErrorKind.RATE_LIMITED and self.retry_after is not None

    def with_earlier(self, earlier: tuple[str, ...]) -> SendResult:
        """This result with ``earlier`` delivered ids in front (after a resumed remainder)."""
        if not earlier:
            return self
        ids = (*earlier, *self.ids)
        return replace(self, message_id=ids[-1], continuation_ids=ids[:-1], partial=not self.ok)


# -- retry ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2
    base_delay: float = 2.0
    jitter: float = 1.0
    # Longest wait slept inline; longer server waits return the typed flood failure.
    inline_wait_cap: float = 60.0


Attempt = Callable[[str, bool], Awaitable[SendResult]]


def _wants_retry(result: SendResult) -> bool:
    return not result.maybe_delivered and (
        result.retryable or result.retry_after is not None or result.kind is ErrorKind.RATE_LIMITED
    )


async def _call(attempt: Attempt, text: str, plain: bool) -> SendResult:
    try:
        return await attempt(text, plain)
    except Exception as exc:  # an adapter that raised instead of answering
        return SendResult.failure(exc=exc)


async def send_with_retry(
    attempt: Attempt,
    content: str,
    *,
    clock: Clock,
    policy: RetryPolicy | None = None,
    rng: Callable[[], float] = random.random,
    notice: str | None = DELIVERY_FAILED_NOTICE,
    is_final: Callable[[SendResult], bool] | None = None,
) -> SendResult:
    """Send ``content`` through ``attempt(text, plain)`` under the retry rules above."""
    policy = policy or RetryPolicy()
    result = await _call(attempt, content, False)
    if result.ok or result.unsupported or (is_final is not None and is_final(result)):
        return result
    if result.maybe_delivered:
        return result

    if _wants_retry(result):
        server_wait = result.retry_after
        for n in range(policy.max_retries):
            if server_wait is not None and server_wait > policy.inline_wait_cap:
                log.warning(
                    "send rate limited for %.0f s (inline cap %.0f s); handing the wait over",
                    server_wait,
                    policy.inline_wait_cap,
                )
                return result
            if result.partial and not result.remainder:
                log.warning("split send partly delivered; the rest cannot be resumed safely")
                return result
            base = server_wait if server_wait is not None else policy.base_delay * (2**n)
            await clock.sleep(min(base + rng() * policy.jitter, max(base, policy.inline_wait_cap)))
            earlier = result.ids if result.partial else ()
            text = result.remainder if result.partial and result.remainder else content
            again = await _call(attempt, text, False)
            if earlier and not (again.ok or again.partial or again.maybe_delivered):
                # Nothing of the remainder landed, so it is still the proven-undelivered tail.
                again = replace(again, remainder=text)
            result = again.with_earlier(earlier)
            if result.ok or result.unsupported or (is_final is not None and is_final(result)):
                return result
            if result.maybe_delivered:
                return result
            server_wait = result.retry_after
            if not _wants_retry(result):
                break
        else:
            if result.kind is ErrorKind.RATE_LIMITED or result.retry_after is not None:
                return result
            log.warning("send failed after %d retries: %s", policy.max_retries, result.error)
            if notice:
                await _call(attempt, notice, True)
            return result

    if result.partial or result.kind not in (ErrorKind.BAD_FORMAT, ErrorKind.UNKNOWN):
        return result
    log.info("send failed (%s); trying once as plain text", result.kind)
    return await _call(attempt, content, True)


__all__ = [
    "DELIVERY_FAILED_NOTICE",
    "Attempt",
    "ErrorKind",
    "RetryPolicy",
    "SendResult",
    "classify_send_error",
    "is_ambiguous_timeout",
    "is_chat_level_not_found",
    "send_with_retry",
]
