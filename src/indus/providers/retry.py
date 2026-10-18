"""Server-named waits, jittered backoff and the retry loop around one model call.

``parse_retry_after`` reads how long a server asked us to wait, in any of the shapes providers
use: a ``Retry-After`` header (seconds or an HTTP date), ``retry-after-ms``, a JSON field, or a
sentence in the error text. ``backoff`` is the jittered exponential wait when the server named
none. ``retry_call`` runs a call up to ``max_retries + 1`` times, retrying only verdicts the
classifier marks retryable and sleeping on an injected clock in short slices so an interrupt
lands quickly. Credential rotation and fallback chains (P08) act on the final ``ClassifiedError``
that ``RetryFailed`` carries.
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

import httpx

from indus.foundation.errors import ProviderError

if TYPE_CHECKING:
    from indus.foundation.clock import Clock
    from indus.providers.classify import ClassifiedError

log = logging.getLogger(__name__)

RETRY_AFTER_CAP = 600.0
BACKOFF_BASE = 2.0
BACKOFF_MAX = 60.0
BACKOFF_JITTER = 0.5
SLEEP_SLICE = 0.2
ANNOUNCE_AFTER = 60.0
DEFAULT_MAX_RETRIES = 3

_UNITS: dict[str, float] = {
    "ms": 0.001,
    "msec": 0.001,
    "msecs": 0.001,
    "millisecond": 0.001,
    "milliseconds": 0.001,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
}
_UNIT_ALT = "|".join(sorted(_UNITS, key=len, reverse=True))
_PART = rf"(\d+(?:\.\d+)?)\s*({_UNIT_ALT})\b"
_DURATION = re.compile(rf"(?:{_PART}[\s,]*(?:and\s+)?)+", re.IGNORECASE)
_PART_RE = re.compile(_PART, re.IGNORECASE)
# Lead-ins after which a duration (or a bare number of seconds) names the wait.
_LEAD_INS = re.compile(
    r"(?:retry|try\s+again)(?:\s+(?:after|in))?|resets?\s+(?:in|after)|"
    r"(?:available|again)\s+in|wait(?:\s+for)?|cool[\s-]?down\s+of",
    re.IGNORECASE,
)
_BARE_SECONDS = re.compile(r"\s*(\d+(?:\.\d+)?)(?![\d.]|\s*[a-z%])", re.IGNORECASE)
_HEADER_SECONDS = ("retry-after-ms", "retry-after")
_JSON_SECONDS = ("retry_after", "retryAfter", "retry_delay", "retryDelay", "resets_in_seconds")
_JSON_MS = ("retry_after_ms", "retryAfterMs")


def parse_retry_after(
    value: Any, *, now: datetime | None = None, cap: float | None = RETRY_AFTER_CAP
) -> float | None:
    """Seconds to wait named by ``value``, or None when it names none.

    ``value`` may be a header mapping, a header value, a number, a JSON mapping / JSON text, or
    free text. HTTP dates are measured from ``now`` (an injected clock's time; the real UTC time
    when omitted). The result is never negative and is capped at ``cap`` (600 s by default;
    ``None`` disables the cap, for computing a quota reset time).
    """
    seconds = _parse(value, now)
    if seconds is None or math.isnan(seconds):
        return None
    seconds = max(0.0, seconds)
    return seconds if cap is None else min(seconds, cap)


def _parse(value: Any, now: datetime | None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, Mapping):
        return _from_mapping(value, now)  # pyright: ignore[reportUnknownArgumentType]
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    number = _number(text)
    if number is not None:
        return number
    if text[0] in "{[":
        try:
            decoded = json.loads(text)
        except ValueError:
            decoded = None
        if isinstance(decoded, Mapping):
            found = _from_mapping(decoded, now)  # pyright: ignore[reportUnknownArgumentType]
            if found is not None:
                return found
    duration = _duration(text)
    if duration is not None and _DURATION.fullmatch(text):
        return duration
    date = _http_date(text, now)
    if date is not None:
        return date
    return _from_text(text)


def _number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _duration(text: str) -> float | None:
    parts = _PART_RE.findall(text)
    if not parts:
        return None
    return sum(float(amount) * _UNITS[unit.lower()] for amount, unit in parts)


def _http_date(text: str, now: datetime | None) -> float | None:
    if not any(ch.isalpha() for ch in text) or ("," not in text and "GMT" not in text):
        return None
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    reference = now if now is not None else datetime.now(UTC)
    return (when - reference).total_seconds()


def _from_text(text: str) -> float | None:
    for lead in _LEAD_INS.finditer(text):
        rest = text[lead.end() :]
        match = _DURATION.match(rest.lstrip())
        if match:
            found = _duration(match.group(0))
            if found is not None:
                return found
        bare = _BARE_SECONDS.match(rest)
        if bare:
            return float(bare.group(1))
    return None


def _from_mapping(data: Mapping[Any, Any], now: datetime | None) -> float | None:
    lowered = {str(k).lower(): v for k, v in data.items()}
    if "retry-after-ms" in lowered:
        ms = _parse(lowered["retry-after-ms"], now)
        if ms is not None:
            return ms / 1000.0
    if "retry-after" in lowered:
        found = _parse(lowered["retry-after"], now)
        if found is not None:
            return found
    for key in _JSON_MS:
        if key in data:
            ms = _parse(data[key], now)
            if ms is not None:
                return ms / 1000.0
    for key in _JSON_SECONDS:
        if key in data:
            found = _parse(data[key], now)
            if found is not None:
                return found
    for nested_key in ("error", "details", "metadata"):
        nested = data.get(nested_key)
        candidates = nested if isinstance(nested, list) else [nested]
        for item in candidates:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(item, Mapping):
                found = _from_mapping(item, now)  # pyright: ignore[reportUnknownArgumentType]
                if found is not None:
                    return found
    message = data.get("message")
    if isinstance(message, str):
        return _from_text(message)
    return None


def backoff(attempt: int, *, rng: random.Random) -> float:
    """Wait before retry number ``attempt`` (1-based): min(2·2^(n−1), 60) plus up to 50 %."""
    exponent = max(0, attempt - 1)
    delay = BACKOFF_MAX if exponent >= 32 else min(BACKOFF_BASE * (2**exponent), BACKOFF_MAX)
    return delay + rng.uniform(0.0, BACKOFF_JITTER * delay)


async def sleep_sliced(clock: Clock, seconds: float, *, slice_seconds: float = SLEEP_SLICE) -> None:
    """Sleep ``seconds`` on ``clock`` in short slices measured against one deadline."""
    if seconds <= 0:
        return
    deadline = clock.monotonic() + seconds
    while (left := deadline - clock.monotonic()) > 0:
        await clock.sleep(min(slice_seconds, left))


# -- the retry loop -------------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryWait:
    """One wait between attempts. ``announced`` is True when it was reported as it started."""

    attempt: int  # the attempt that failed, 1-based
    attempts: int  # total attempts allowed
    seconds: float
    error: ClassifiedError
    from_server: bool
    announced: bool

    def describe(self) -> str:
        source = "the server asked for" if self.from_server else "backing off"
        return (
            f"attempt {self.attempt}/{self.attempts} failed ({self.error.reason}): "
            f"{self.error.message}; {source} {self.seconds:.1f}s"
        )


class RetryFailed(ProviderError):
    """Every attempt failed, or the first failure was not worth retrying."""

    def __init__(
        self,
        classified: ClassifiedError,
        *,
        error: BaseException,
        attempts: int,
        waits: tuple[RetryWait, ...],
    ) -> None:
        super().__init__(classified.message or classified.reason)
        self.classified = classified
        self.error = error
        self.attempts = attempts
        self.waits = waits


def clamp_max_retries(value: object) -> int:
    """``agent.api_max_retries``: an integer, at least 1 (bad values give the default 3)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_MAX_RETRIES
    return max(1, value)


async def retry_call[T](
    fn: Callable[[], Awaitable[T]],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    clock: Clock,
    on_wait: Callable[[RetryWait], object] | None = None,
    classify: Callable[[BaseException], ClassifiedError] | None = None,
    rng: random.Random | None = None,
) -> T:
    """Call ``fn`` until it succeeds, a failure is not retryable, or the attempts run out.

    Waits up to 60 s are buffered and handed to ``on_wait`` only when every attempt failed; a
    longer wait is handed over as it starts. Cancellation during a wait propagates at once.
    """
    if classify is None:
        from indus.providers.classify import classify as default_classify

        classify = default_classify
    rng = rng if rng is not None else random.Random()
    retries = max(1, max_retries)
    total = retries + 1
    buffered: list[RetryWait] = []
    waits: list[RetryWait] = []
    attempt = 0
    while True:
        attempt += 1
        try:
            return await fn()
        except RetryFailed:
            raise
        except _RETRYABLE_TYPES as exc:  # anything else is a bug and propagates untouched
            error: BaseException = exc
        verdict = classify(error)
        if not verdict.retryable or attempt >= total:
            for wait in buffered:
                _emit(on_wait, wait)
            log.info(
                "model call failed after %d attempt(s): %s (%s)",
                attempt,
                verdict.reason,
                verdict.message,
            )
            raise RetryFailed(verdict, error=error, attempts=attempt, waits=tuple(waits)) from error
        from_server = verdict.retry_after is not None
        seconds = (
            verdict.retry_after if verdict.retry_after is not None else backoff(attempt, rng=rng)
        )
        announce = seconds > ANNOUNCE_AFTER
        wait = RetryWait(attempt, total, seconds, verdict, from_server, announce)
        waits.append(wait)
        log.debug("%s", wait.describe())
        if announce:
            _emit(on_wait, wait)
        else:
            buffered.append(wait)
        await sleep_sliced(clock, seconds)


# Failures a model call can legitimately produce; the classifier decides which are retried.
_RETRYABLE_TYPES = (ProviderError, httpx.HTTPError, OSError, TimeoutError)


def _emit(on_wait: Callable[[RetryWait], object] | None, wait: RetryWait) -> None:
    if on_wait is None:
        return
    try:
        on_wait(wait)
    except Exception:  # a display hook must never break the call
        log.debug("retry wait callback failed", exc_info=True)


__all__ = [
    "ANNOUNCE_AFTER",
    "RETRY_AFTER_CAP",
    "RetryFailed",
    "RetryWait",
    "backoff",
    "clamp_max_retries",
    "parse_retry_after",
    "retry_call",
    "sleep_sliced",
]
