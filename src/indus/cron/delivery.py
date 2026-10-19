"""Sending a run's result: the deliverer registry, local delivery, redaction and the status split.

``deliver(scope, job, text, …)`` picks the lane (``failure_deliver`` for failure notices when the
job has one, else ``deliver``), parses it with ``cron.targets``, resolves ``origin`` from the job's
stored origin (``local`` when there is none), masks secrets, wraps the text and hands one
``DeliveryRequest`` per target to the deliverer registered for its platform. This phase registers
``local`` only: the output document the runner already wrote is the delivery, plus an optional
local notification (``cron.local_notify``). The gateway (P15-20) registers one deliverer per
connected platform, and one for ``all``, with ``register_deliverer``.

Rules:

- an empty payload is never sent (status ``suppressed``);
- secrets are masked on every lane whatever the log-redaction setting is; if masking fails the
  payload is replaced by ``REDACTION_FAILED_NOTICE`` rather than sent unscanned; the job name in
  the header is masked too;
- the default wrapper puts a header naming the job above the text and a footer saying the agent
  cannot see this message (``cron.wrap_response: false`` sends the text alone);
- attachments are only ``Path`` objects the runner hands in, resolved and required to sit inside
  the job's own output folder and to pass the P05 read guard; file paths are never taken from the
  reply text, so a path the model (or an MCP server) writes can never be attached;
- the result's status (``delivered``, ``delivery_failed`` with a reason, ``unverified``,
  ``suppressed``) is separate from the run's status: a delivery failure never counts toward the
  job's failure streak (R07.17).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any, Literal

from indus.cron import ports, targets
from indus.cron.targets import DeliveryTarget
from indus.foundation.scope import Scope
from indus.safety import path_guards
from indus.safety.redaction import redact_output

__all__ = [
    "REDACTION_FAILED_NOTICE",
    "SEND_TIMEOUT_S",
    "DeliveryReceipt",
    "DeliveryRequest",
    "DeliveryResult",
    "TargetOutcome",
    "deliver",
    "deliverer_for",
    "redact_payload",
    "register_deliverer",
    "registered_platforms",
    "set_local_notifier",
    "trusted_attachments",
    "wrap",
]

log = logging.getLogger(__name__)

REDACTION_FAILED_NOTICE = (
    "[Indus withheld this scheduled-job message: secret masking failed, so the text was not sent. "
    "The full output is in the job's output folder.]"
)
SEND_TIMEOUT_S = 60.0
"""Upper bound for one deliverer call; a slower send counts as failed for that target."""

type DeliveryStatus = Literal["delivered", "delivery_failed", "unverified", "suppressed"]
type OutcomeStatus = Literal["delivered", "failed", "unverified"]


@dataclass(frozen=True)
class DeliveryRequest:
    """One send. ``text`` is masked and wrapped; ``plain_text`` is masked and unwrapped."""

    scope: Scope
    job_id: str
    job_name: str
    target: DeliveryTarget
    text: str
    plain_text: str
    for_failure: bool = False
    output_path: Path | None = None
    attachments: tuple[Path, ...] = ()


@dataclass(frozen=True)
class DeliveryReceipt:
    """What a deliverer reports. ``verified`` False: the platform accepted the send without
    giving evidence (no message id), so the status becomes ``unverified``."""

    ok: bool
    verified: bool = True
    error: str | None = None
    message_id: str | None = None


@dataclass(frozen=True)
class TargetOutcome:
    target: str
    status: OutcomeStatus
    reason: str | None = None


@dataclass(frozen=True)
class DeliveryResult:
    status: DeliveryStatus
    reason: str | None = None
    outcomes: tuple[TargetOutcome, ...] = ()
    dropped_attachments: tuple[str, ...] = ()

    @property
    def failed(self) -> bool:
        return self.status == "delivery_failed"

    @property
    def unverified_targets(self) -> tuple[str, ...]:
        return tuple(o.target for o in self.outcomes if o.status == "unverified")


type Deliverer = Callable[[DeliveryRequest], Awaitable[DeliveryReceipt]]
type Notifier = Callable[[str, str], None]

# -- the registry ---------------------------------------------------------------------------------

_lock = threading.Lock()
_deliverers: dict[str, Deliverer] = {}


def register_deliverer(platform: str, fn: Deliverer) -> Callable[[], None]:
    """Route targets on ``platform`` (or ``all``) to ``fn``; returns the undo.

    A second registration for the same platform replaces the first (a reconnecting adapter).
    """
    key = platform.strip().lower()
    if not key or key == targets.ORIGIN:
        raise ValueError(f"cannot register a deliverer for {platform!r}")
    with _lock:
        _deliverers[key] = fn

    def undo() -> None:
        with _lock:
            if _deliverers.get(key) is fn:
                del _deliverers[key]

    return undo


def deliverer_for(platform: str) -> Deliverer | None:
    with _lock:
        return _deliverers.get(platform.strip().lower())


def registered_platforms() -> list[str]:
    with _lock:
        return sorted(p for p in _deliverers if p not in targets.RESERVED)


targets.set_live_platforms(registered_platforms)

# -- masking and wrapping -------------------------------------------------------------------------

REDACTOR: Callable[[str, Scope], str] = lambda text, scope: redact_output(  # noqa: E731
    text, force=True, scope=scope
)
"""The masking step (tests replace it to make it fail)."""


def redact_payload(text: str, scope: Scope, what: str = "payload") -> str:
    """``text`` with secrets masked; ``REDACTION_FAILED_NOTICE`` if masking raised."""
    if not text:
        return text
    try:
        return REDACTOR(text, scope)
    except Exception as exc:
        log.warning(
            "cron %s: secret masking failed (%s); sending a notice", what, type(exc).__name__
        )
        return REDACTION_FAILED_NOTICE


def wrap(job_name: str, job_id: str, text: str) -> str:
    """Our default framing: who sent this, then the text, then how to manage the job."""
    return (
        f"Scheduled job: {job_name} [{job_id}]\n\n"
        f"{text}\n\n"
        "-- Sent by the Indus scheduler. The agent does not see this message; to change, pause "
        f"or remove the job, use `indus cron edit|pause|remove {job_id}` or ask in a chat."
    )


# -- local delivery -------------------------------------------------------------------------------


def _terminal_notifier(title: str, line: str) -> None:
    try:
        if sys.stderr.isatty():
            sys.stderr.write(f"\a[indus cron] {title}: {line}\n")
            sys.stderr.flush()
    except (OSError, ValueError):
        pass


_notifier: Notifier = _terminal_notifier


def set_local_notifier(fn: Notifier | None) -> Notifier:
    """Replace the local notification hook (None restores the terminal line); returns the old."""
    global _notifier
    old = _notifier
    _notifier = fn if fn is not None else _terminal_notifier
    return old


async def _deliver_local(request: DeliveryRequest) -> DeliveryReceipt:
    path = request.output_path
    if path is None or not path.is_file():
        return DeliveryReceipt(ok=False, error="the output document was not written")
    if ports.flag_setting(request.scope, "local_notify", False):
        first = next((ln.strip() for ln in request.plain_text.splitlines() if ln.strip()), "")
        try:
            _notifier(request.job_name, first[:200])
        except Exception:
            log.debug("local cron notification failed", exc_info=True)
    return DeliveryReceipt(ok=True, message_id=str(path))


register_deliverer(targets.LOCAL, _deliver_local)

# -- attachments ----------------------------------------------------------------------------------


def trusted_attachments(
    scope: Scope, job_id: str, paths: Iterable[object]
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """``(kept, refused reasons)``: only ``Path`` objects inside the job's output folder that the
    P05 read guard allows. Text (a string path) is refused: it may come from model output."""
    root = ports.output_dir(scope, job_id)
    try:
        root_real = root.resolve(strict=False)
    except OSError:
        return (), tuple(f"{p}: the output folder cannot be resolved" for p in paths)
    kept: list[Path] = []
    refused: list[str] = []
    for item in paths:
        if not isinstance(item, Path):
            refused.append(f"{item!r}: only files the scheduler wrote can be attached")
            continue
        try:
            real = item.resolve(strict=True)
        except (OSError, RuntimeError):
            refused.append(f"{item.name}: not found")
            continue
        if not real.is_relative_to(root_real) or not real.is_file():
            refused.append(f"{item.name}: outside the job's output folder")
            continue
        denial = path_guards.read_denial(os.fspath(real), scope)
        if denial is not None:
            refused.append(f"{item.name}: {denial}")
            continue
        kept.append(real)
    return tuple(kept), tuple(refused)


# -- delivering -----------------------------------------------------------------------------------


@dataclass
class _Plan:
    sends: list[DeliveryTarget] = dc_field(default_factory=list[DeliveryTarget])
    failures: list[TargetOutcome] = dc_field(default_factory=list[TargetOutcome])


def lane_value(job: Any, *, for_failure: bool) -> str:
    """The ``deliver`` text for this notice: ``failure_deliver`` for failures when set."""
    if for_failure:
        failure_lane = ports.field(job, "failure_deliver")
        if isinstance(failure_lane, str | list) and targets.split_tokens(failure_lane):  # pyright: ignore[reportUnknownArgumentType]
            return targets.join_tokens(targets.split_tokens(failure_lane))  # pyright: ignore[reportUnknownArgumentType]
    raw = ports.field(job, "deliver", targets.LOCAL)
    tokens = targets.split_tokens(raw if isinstance(raw, str | list) else str(raw))  # pyright: ignore[reportUnknownArgumentType]
    return targets.join_tokens(tokens) or targets.LOCAL


def plan_targets(job: Any, value: str) -> _Plan:
    """Parse ``value`` and resolve ``origin`` for ``job``; problems become failed outcomes."""
    parsed = targets.parse_targets(value)
    plan = _Plan()
    for problem in parsed.problems:
        plan.failures.append(
            TargetOutcome(problem.token, "failed", f"{problem.code}: {problem.message}")
        )
    seen: set[tuple[str, str | None, str | None]] = set()
    for target in parsed.targets:
        resolved = target
        if target.is_origin:
            resolved = targets.origin_target(ports.origin_of(job)) or DeliveryTarget(
                targets.LOCAL, raw=targets.ORIGIN
            )
        if resolved.key in seen:
            continue
        seen.add(resolved.key)
        plan.sends.append(resolved)
    return plan


async def _send(deliverer: Any, request: DeliveryRequest) -> TargetOutcome:
    where = request.target.render()
    try:
        receipt = await asyncio.wait_for(deliverer(request), SEND_TIMEOUT_S)
    except TimeoutError:
        return TargetOutcome(where, "failed", f"no answer within {SEND_TIMEOUT_S:g} s")
    except Exception as exc:
        detail = redact_payload(str(exc), request.scope, "delivery error")[:300]
        return TargetOutcome(where, "failed", f"{type(exc).__name__}: {detail}".rstrip(": "))
    if not isinstance(receipt, DeliveryReceipt):
        return TargetOutcome(where, "failed", "the deliverer gave no receipt")
    if not receipt.ok:
        detail = redact_payload(receipt.error or "refused", request.scope, "delivery error")
        return TargetOutcome(where, "failed", detail[:300])
    if not receipt.verified:
        return TargetOutcome(where, "unverified", "accepted without confirmation")
    return TargetOutcome(where, "delivered")


def _missing_deliverer(target: DeliveryTarget) -> str:
    if target.is_broadcast:
        return "`all` needs the gateway: no connected platform to deliver to"
    return f"platform delivery needs the gateway (nothing delivers to {target.platform} yet)"


async def deliver(
    scope: Scope,
    job: Any,
    text: str,
    *,
    for_failure: bool = False,
    output_path: Path | None = None,
    attachments: Sequence[object] = (),
) -> DeliveryResult:
    """Deliver ``text`` for ``job`` (see the module notes)."""
    if not text or not text.strip():
        return DeliveryResult("suppressed", "nothing to send")
    job_id = str(ports.field(job, "id", "") or "")
    raw_name = str(ports.field(job, "name", "") or job_id or "cron job")
    name = redact_payload(raw_name, scope, "job name")
    plain = redact_payload(text.strip(), scope, "delivery")
    framed = (
        wrap(name, job_id, plain) if ports.flag_setting(scope, "wrap_response", True) else plain
    )
    kept, refused = trusted_attachments(scope, job_id, attachments) if attachments else ((), ())
    plan = plan_targets(job, lane_value(job, for_failure=for_failure))
    outcomes = list(plan.failures)
    for target in plan.sends:
        fn = deliverer_for(target.platform)
        if fn is None:
            outcomes.append(TargetOutcome(target.render(), "failed", _missing_deliverer(target)))
            continue
        request = DeliveryRequest(
            scope=scope,
            job_id=job_id,
            job_name=name,
            target=target,
            text=framed,
            plain_text=plain,
            for_failure=for_failure,
            output_path=output_path,
            attachments=kept,
        )
        outcomes.append(await _send(fn, request))
    return _summarise(outcomes, refused)


def _summarise(outcomes: list[TargetOutcome], refused: tuple[str, ...]) -> DeliveryResult:
    reasons = [f"{o.target}: {o.reason}" for o in outcomes if o.status == "failed"]
    if refused:
        reasons.append(f"{len(refused)} attachment(s) refused: " + "; ".join(refused))
    if reasons:
        return DeliveryResult("delivery_failed", "; ".join(reasons), tuple(outcomes), refused)
    if not outcomes:
        return DeliveryResult("delivery_failed", "no delivery target", (), refused)
    if any(o.status == "unverified" for o in outcomes):
        return DeliveryResult("unverified", "a platform gave no confirmation", tuple(outcomes))
    return DeliveryResult("delivered", None, tuple(outcomes))
