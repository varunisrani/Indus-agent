"""Webhook route rules: the route model, payload filters, prompt templates, coalescing, and the
per-route rate window and delivery-id memory (P15-32).

Routes (``RouteSpec``) come from ``gateway.webhook.routes`` and from the subscriptions file;
``check_route`` refuses a route that could not be served safely, before the receiver starts:
no secret and not ``insecure``; ``insecure`` on a host that is not loopback; ``deliver_only``
without a target; ``deliver_only`` together with ``coalesce``.

Filters are mappings: ``{"field": "a.b", "equals": v}``, ``{"field": ..., "contains": v}`` (a
substring of the text, or a member of a list), ``{"field": ..., "exists": true|false}``, combined
with ``{"all": [...]}``, ``{"any": [...]}`` and ``{"not": {...}}``. A field is a dot path into the
payload (digits index lists); ``event`` is the event name. A route's filter list must all match.

Templates replace ``{dot.path}`` with the payload value there (text as is, numbers and booleans
as written, objects and lists as JSON, each value at most ``VALUE_MAX`` characters), ``{event}``
with the event name and ``{payload}`` with the whole payload as JSON. A key that is not there
renders as empty text, never an error. An empty template gives a default prompt naming the event
with the payload as JSON.

Coalescing (``Coalescer``): events whose ``coalesce.key`` renders to the same value are held; each
new one replaces the held one and restarts the quiet window, but the group is released no later
than ``max_wait_seconds`` after its first event. Only the newest event runs, with a note that
earlier ones were folded in. A key that does not resolve does not coalesce. Timers use the
injected clock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from indus.foundation.clock import Clock
from indus.gateway.http.serving import is_loopback
from indus.state.webhook_subscriptions import ROUTE_NAME_RE

__all__ = [
    "IDEMPOTENCY_TTL_S",
    "RATE_WINDOW_S",
    "Coalescer",
    "CoalesceSpec",
    "DeliveryMemory",
    "PendingEvent",
    "RateWindow",
    "RouteError",
    "RouteSpec",
    "check_route",
    "filter_matches",
    "filters_match",
    "load_route",
    "render",
    "resolve",
]

log = logging.getLogger(__name__)

RATE_WINDOW_S = 60.0
IDEMPOTENCY_TTL_S = 3_600.0
VALUE_MAX = 4_000
_PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_.-]+)\}")
_MISSING = object()

type AuthScheme = Literal["auto", "github", "gitlab", "hmac", "hmac_ts"]


class RouteError(ValueError):
    """A route that cannot be served; the receiver refuses to load it."""


class CoalesceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    window_seconds: float = Field(default=30.0, gt=0)
    max_wait_seconds: float = Field(default=300.0, gt=0)


class RouteSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret: str | None = None
    auth: AuthScheme = "auto"
    insecure: bool = False
    enabled: bool = True
    events: list[str] = Field(default_factory=list[str])
    filters: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])
    prompt: str = ""
    deliver: str | None = None
    deliver_only: bool = False
    coalesce: CoalesceSpec | None = None
    description: str = ""
    created_at: str | None = None


def load_route(name: str, raw: object) -> RouteSpec:
    """``raw`` as a ``RouteSpec`` (``RouteError`` naming the route and the bad field)."""
    try:
        return RouteSpec.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "route"
        raise RouteError(f"webhook route {name!r}: {where}: {first['msg']}") from None


def check_route(name: str, spec: RouteSpec, host: str | None) -> None:
    """See the module notes; raises ``RouteError``."""
    if not ROUTE_NAME_RE.match(name):
        raise RouteError(f"webhook route {name!r}: names are lower-case letters, digits, - and _")
    if spec.insecure:
        if not is_loopback(host):
            raise RouteError(
                f"webhook route {name!r} is marked insecure (no signature check), which is only "
                f"allowed while the receiver listens on loopback, not on {host!r}"
            )
    elif not (spec.secret or "").strip():
        raise RouteError(
            f"webhook route {name!r} has no secret: set 'secret' to the name of a stored secret "
            "(indus webhook subscribe makes one), or 'insecure: true' for a loopback-only test"
        )
    if spec.deliver_only and not (spec.deliver or "").strip():
        raise RouteError(f"webhook route {name!r}: deliver_only needs a 'deliver' target")
    if spec.deliver_only and spec.coalesce is not None:
        raise RouteError(f"webhook route {name!r}: coalesce only applies to agent routes")


# -- paths, filters, templates ------------------------------------------------------------------


def resolve(payload: object, path: str, event: str = "") -> object:
    """The value at dot ``path`` in ``payload`` (``event`` is the event name), else ``_MISSING``."""
    parts = [p for p in path.strip().split(".") if p]
    if not parts:
        return _MISSING
    if parts == ["event"]:
        return event
    value: object = payload
    for part in parts:
        if isinstance(value, Mapping):
            value = cast(Mapping[str, object], value).get(part, _MISSING)
        elif isinstance(value, list) and part.isdigit():
            items = cast(list[object], value)
            index = int(part)
            value = items[index] if index < len(items) else _MISSING
        else:
            return _MISSING
        if value is _MISSING:
            return _MISSING
    return value


def _as_text(value: object) -> str:
    if value is _MISSING or value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, int | float):
        text = str(value)
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= VALUE_MAX else text[:VALUE_MAX] + "…"


def filter_matches(spec: object, payload: object, event: str) -> bool:
    if not isinstance(spec, Mapping):
        return False
    rule = cast(Mapping[str, Any], spec)
    if "all" in rule:
        items = rule["all"]
        return isinstance(items, list) and all(
            filter_matches(i, payload, event) for i in cast(list[Any], items)
        )
    if "any" in rule:
        items = rule["any"]
        return isinstance(items, list) and any(
            filter_matches(i, payload, event) for i in cast(list[Any], items)
        )
    if "not" in rule:
        return not filter_matches(rule["not"], payload, event)
    path = rule.get("field")
    if not isinstance(path, str):
        return False
    value = resolve(payload, path, event)
    if "exists" in rule:
        return (value is not _MISSING) is bool(rule["exists"])
    if value is _MISSING:
        return False
    if "equals" in rule:
        return value == rule["equals"]
    if "contains" in rule:
        needle = rule["contains"]
        if isinstance(value, list):
            return needle in cast(list[object], value)
        return _as_text(needle) in _as_text(value)
    return False


def filters_match(filters: list[dict[str, Any]], payload: object, event: str) -> bool:
    return all(filter_matches(spec, payload, event) for spec in filters)


def render(template: str, payload: object, event: str, route: str) -> str:
    """See the module notes."""
    if not template.strip():
        body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)[:VALUE_MAX]
        return f"Webhook event '{event}' on route '{route}':\n\n```json\n{body}\n```"

    def one(match: re.Match[str]) -> str:
        key = match.group(1)
        if key == "event":
            return event
        if key == "payload":
            return _as_text(payload)
        return _as_text(resolve(payload, key, event))

    return _PLACEHOLDER.sub(one, template)


def coalesce_key(spec: CoalesceSpec, payload: object, event: str) -> str | None:
    """The group key of an event, or None when any part of it is missing."""
    template = spec.key.strip()
    keys = _PLACEHOLDER.findall(template) if "{" in template else [template]
    if not keys or any(
        k not in {"event", "payload"} and resolve(payload, k, event) is _MISSING for k in keys
    ):
        return None
    if "{" not in template:
        return _as_text(resolve(payload, template, event))
    return render(template, payload, event, "")


# -- rate window and delivery ids ---------------------------------------------------------------


@dataclass
class RateWindow:
    """At most ``limit`` hits per route in any ``RATE_WINDOW_S`` (monotonic clock)."""

    clock: Clock
    limit: Callable[[], int]
    hits: dict[str, deque[float]] = field(default_factory=dict[str, deque[float]])

    def allow(self, route: str) -> bool:
        now = self.clock.monotonic()
        window = self.hits.setdefault(route, deque())
        while window and window[0] <= now - RATE_WINDOW_S:
            window.popleft()
        if len(window) >= self.limit():
            return False
        window.append(now)
        return True


@dataclass
class DeliveryMemory:
    """Delivery ids seen per route in the last ``IDEMPOTENCY_TTL_S``."""

    clock: Clock
    seen: dict[tuple[str, str], float] = field(default_factory=dict[tuple[str, str], float])
    cap: int = 10_000

    def first_time(self, route: str, delivery_id: str) -> bool:
        """True (and remembered) the first time; False for a repeat inside the window."""
        now = self.clock.monotonic()
        key = (route, delivery_id)
        when = self.seen.get(key)
        if when is not None and now - when < IDEMPOTENCY_TTL_S:
            return False
        self.seen[key] = now
        if len(self.seen) > self.cap:
            self.prune(now)
        return True

    def forget(self, route: str, delivery_id: str) -> None:
        self.seen.pop((route, delivery_id), None)

    def prune(self, now: float) -> None:
        for key, when in list(self.seen.items()):
            if now - when >= IDEMPOTENCY_TTL_S:
                del self.seen[key]
        while len(self.seen) > self.cap:
            self.seen.pop(next(iter(self.seen)))


# -- coalescing ---------------------------------------------------------------------------------


@dataclass
class PendingEvent:
    route: str
    payload: object
    prompt: str
    event: str
    delivery_id: str
    first_at: float
    count: int = 1
    folded: tuple[str, ...] = ()

    def prompt_with_note(self) -> str:
        if self.count <= 1:
            return self.prompt
        return (
            f"{self.prompt}\n\n(Note: {self.count} events about this item arrived close together; "
            "this is the newest one and the earlier ones were folded into it.)"
        )


type Dispatch = Callable[[PendingEvent], Awaitable[object] | object]


class Coalescer:
    """See the module notes. ``dispatch`` runs the released event (one agent turn)."""

    def __init__(self, clock: Clock, dispatch: Dispatch) -> None:
        self.clock = clock
        self.dispatch = dispatch
        self.pending: dict[str, PendingEvent] = {}
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    def offer(
        self,
        route: str,
        spec: CoalesceSpec,
        payload: object,
        event: str,
        prompt: str,
        delivery_id: str,
    ) -> bool:
        """Hold the event; False when its key does not resolve (run it now instead)."""
        key = coalesce_key(spec, payload, event)
        if key is None:
            return False
        group = f"{route}|{key}"
        now = self.clock.monotonic()
        held = self.pending.get(group)
        first_at = held.first_at if held is not None else now
        self.pending[group] = PendingEvent(
            route=route,
            payload=payload,
            prompt=prompt,
            event=event,
            delivery_id=delivery_id,
            first_at=first_at,
            count=held.count + 1 if held is not None else 1,
            folded=(*held.folded, held.delivery_id) if held is not None else (),
        )
        old = self._timers.pop(group, None)
        if old is not None:
            old.cancel()
        due = now + min(spec.window_seconds, max(0.0, first_at + spec.max_wait_seconds - now))
        self._timers[group] = asyncio.ensure_future(self._release_at(group, due))
        return True

    async def _release_at(self, group: str, due: float) -> None:
        # The deadline was fixed when the event came in; a timer that starts late waits less.
        delay = due - self.clock.monotonic()
        if delay > 0:
            await self.clock.sleep(delay)
        self._timers.pop(group, None)
        held = self.pending.pop(group, None)
        if held is not None:
            await self._run(held)

    async def _run(self, held: PendingEvent) -> None:
        try:
            outcome = self.dispatch(held)
            if asyncio.iscoroutine(outcome):
                await outcome
        except Exception:
            log.warning("a coalesced webhook event failed to start", exc_info=True)

    async def flush(self) -> None:
        """Release every held group now (shutdown); nothing held is dropped."""
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        held, self.pending = self.pending, {}
        for event in held.values():
            await self._run(event)

    def drop(self) -> list[PendingEvent]:
        """Stop every timer and hand back the held events without running them."""
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        held, self.pending = self.pending, {}
        return list(held.values())
