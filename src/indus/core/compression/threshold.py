"""When a conversation is big enough to compress: the trigger arithmetic (P08-02, R01.7).

``trigger_tokens(window, max_output, percent, cap=…, aux_window=…)``:

1. the usable input window is ``window − max_output``; the whole window when no reservation is
   set or the reservation leaves nothing;
2. windows under 512,000 tokens use at least 0.75 (the floor only ever raises the percent);
3. the trigger is ``max(usable × percent, 64,000)``; when the 64,000 minimum is what decided it,
   it is held to 85 % of the usable window; a trigger at or above the usable window becomes 85 %
   (an explicit percent above 0.85 is the user's choice and stays);
4. an absolute cap (``compression.threshold_tokens``) and a smaller summariser window each lower
   the result.

The developer guide's "200K window → 100K trigger" example forgets step 2; the code (and we) give
150K.

``resolve_percent`` picks a per-model override: the longest key contained in the model id wins;
a ``provider:substring`` key counts only for that provider and beats a bare key of the same
substring. ``CompressionSettings.from_scope`` reads the ``compression`` config section.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from indus.foundation.scope import Scope

log = logging.getLogger(__name__)

__all__ = [
    "MIN_TRIGGER",
    "MIN_TRIGGER_SHARE",
    "SMALL_WINDOW",
    "SMALL_WINDOW_FLOOR",
    "CompressionSettings",
    "effective_window",
    "effective_percent",
    "resolve_percent",
    "trigger_tokens",
]

MIN_TRIGGER = 64_000
MIN_TRIGGER_SHARE = 0.85
SMALL_WINDOW = 512_000
SMALL_WINDOW_FLOOR = 0.75
DEFAULT_PERCENT = 0.50
MAX_ATTEMPTS_LIMIT = 10

TailMode = Literal["lean", "legacy"]


def _positive(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = int(value)
    return number if number > 0 else None


def effective_window(window: int, max_output: int | None) -> int:
    """Tokens the prompt may use. ``max_output`` is carved out of ``window``; with no positive
    reservation, or one as large as the window, all of ``window`` counts."""
    reserve = _positive(max_output) or 0
    usable = window - reserve
    return usable if usable > 0 else window


def effective_percent(window: int, percent: float) -> float:
    """``percent`` raised to 0.75 for windows under 512,000 tokens (never lowered)."""
    if 0 < window < SMALL_WINDOW:
        return max(percent, SMALL_WINDOW_FLOOR)
    return percent


def trigger_tokens(
    window: int,
    max_output: int | None,
    percent: float,
    *,
    cap: int | None = None,
    aux_window: int | None = None,
) -> int:
    """The prompt size (tokens) at which compression fires; 0 when the window is unknown."""
    if window <= 0:
        return 0
    usable = effective_window(window, max_output)
    share = effective_percent(window, percent)
    from_share = int(usable * share)
    trigger = max(from_share, MIN_TRIGGER)
    held = int(usable * MIN_TRIGGER_SHARE)
    if trigger > from_share and trigger > held:
        # The 64K minimum decided it: keep output headroom on small windows.
        trigger = max(from_share, held)
    if trigger >= usable:
        trigger = max(1, min(held, usable - 1))
    limit = _positive(cap)
    if limit is not None and limit < trigger:
        trigger = limit
    aux = _positive(aux_window)
    if aux is not None and aux < trigger:
        trigger = aux
    return max(1, trigger)


def _key_rank(key: str, model: str, provider: str) -> tuple[int, int] | None:
    """``(substring length, scoped)`` when ``key`` applies to ``model`` on ``provider``."""
    text = key.strip().lower()
    scoped = 0
    if ":" in text:
        wanted, _, text = text.partition(":")
        if not provider or wanted.strip() != provider:
            return None
        text = text.strip()
        scoped = 1
    if not text or text not in model:
        return None
    return len(text), scoped


def resolve_percent(
    model: str, provider: str, overrides: Mapping[str, float] | None, default: float
) -> float:
    """The override whose key is the longest substring of ``model`` (provider-scoped keys win a
    tie), else ``default``. Unusable values are skipped."""
    if not overrides or not model:
        return default
    lowered = model.lower()
    who = provider.strip().lower()
    best: tuple[tuple[int, int], float] | None = None
    for key, value in overrides.items():
        rank = _key_rank(str(key), lowered, who)
        if rank is None:
            continue
        try:
            share = float(value)
        except (TypeError, ValueError):
            log.warning("compression.model_thresholds[%r] is not a number; ignored", key)
            continue
        if not 0 < share <= 1:
            log.warning("compression.model_thresholds[%r] must be in (0, 1]; ignored", key)
            continue
        if best is None or rank > best[0]:
            best = (rank, share)
    return best[1] if best is not None else default


def _float(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _int(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


@dataclass(frozen=True)
class CompressionSettings:
    """The ``compression`` config section, cleaned."""

    enabled: bool = True
    threshold: float = DEFAULT_PERCENT
    threshold_tokens: int | None = None
    model_thresholds: Mapping[str, float] = field(default_factory=dict[str, float])
    max_attempts: int = 3
    timeout_s: float = 120.0
    tail_mode: TailMode = "lean"
    target_ratio: float = 0.20
    min_tail_user_messages: int = 1
    in_place: bool = True
    abort_on_summary_failure: bool = False
    idle_after_s: int = 0
    engine: str = "builtin"

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_attempts", max(1, min(self.max_attempts, MAX_ATTEMPTS_LIMIT)))
        object.__setattr__(self, "timeout_s", max(1.0, self.timeout_s))
        object.__setattr__(self, "min_tail_user_messages", max(0, self.min_tail_user_messages))
        object.__setattr__(self, "target_ratio", max(0.10, min(self.target_ratio, 0.80)))
        if not 0 < self.threshold <= 1:
            object.__setattr__(self, "threshold", DEFAULT_PERCENT)

    def percent_for(self, model: str, provider: str = "") -> float:
        return resolve_percent(model, provider, self.model_thresholds, self.threshold)

    def trigger(
        self,
        *,
        window: int,
        max_output: int | None,
        model: str,
        provider: str = "",
        aux_window: int | None = None,
    ) -> int:
        return trigger_tokens(
            window,
            max_output,
            self.percent_for(model, provider),
            cap=self.threshold_tokens,
            aux_window=aux_window,
        )

    @classmethod
    def from_scope(cls, scope: Scope | None) -> CompressionSettings:
        config = scope.config if scope is not None else None
        if config is None:
            return cls()

        def get(key: str) -> object:
            try:
                return config.get(key)
            except Exception:
                log.debug("config key %s unreadable; using the default", key, exc_info=True)
                return None

        base = cls()
        raw_overrides = get("compression.model_thresholds")
        overrides: dict[str, float] = {}
        if isinstance(raw_overrides, Mapping):
            for key, value in raw_overrides.items():  # pyright: ignore[reportUnknownVariableType]
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    overrides[str(key)] = float(value)  # pyright: ignore[reportUnknownArgumentType]
        mode = get("compression.tail_mode")
        engine = get("context.engine")
        enabled = get("compression.enabled")
        in_place = get("compression.in_place")
        abort = get("compression.abort_on_summary_failure")
        return cls(
            enabled=enabled if isinstance(enabled, bool) else base.enabled,
            threshold=_float(get("compression.threshold"), base.threshold),
            threshold_tokens=_positive(get("compression.threshold_tokens")),
            model_thresholds=overrides,
            max_attempts=_int(get("compression.max_attempts"), base.max_attempts),
            timeout_s=_float(get("compression.timeout_s"), base.timeout_s),
            tail_mode="legacy" if mode == "legacy" else "lean",
            target_ratio=_float(get("compression.target_ratio"), base.target_ratio),
            min_tail_user_messages=_int(
                get("compression.min_tail_user_messages"), base.min_tail_user_messages
            ),
            in_place=in_place if isinstance(in_place, bool) else base.in_place,
            abort_on_summary_failure=abort if isinstance(abort, bool) else False,
            idle_after_s=_int(get("compression.idle_after_s"), 0),
            engine=engine.strip() if isinstance(engine, str) and engine.strip() else "builtin",
        )
