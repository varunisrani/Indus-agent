"""Keeps Microsoft Graph change-notification subscriptions alive (P16-12).

Graph subscriptions expire (an hour for chat messages, days for other resources), so the receiver
owns the ones it was configured with. For each `SubscriptionSpec` a `SubscriptionKeeper` task:

1. reuses the subscription stored for the spec (``<home>/gateway/msgraph_subscriptions.json``,
   0600) while it has not expired, else creates one (``POST /subscriptions`` with the notification
   URL as both the notification and the lifecycle URL, the clientState secret, the requested
   expiry and TLS 1.2 as the floor) — Graph checks the URL with its validation call, which the
   receiver's listener answers, so the keeper starts only after the listener is up;
2. sleeps until 80 % of the lifetime Graph granted has passed, then renews it (``PATCH
   /subscriptions/{id}`` with a new expiry); a 404 means Graph dropped it, so it is created again;
3. retries a failed create or renewal after 30 s, doubling to 15 minutes, but never sleeps past
   the subscription's expiry;
4. wakes early when Graph sends a lifecycle event: ``reauthorizationRequired`` renews at once,
   ``subscriptionRemoved`` creates a new one.

Tokens come from the client-credentials grant (`ClientCredentials`, Graph scope); requests go only
to the Graph base URL (HTTPS). Nothing here logs a token, the client secret or the clientState.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.fsio import atomic_write_json
from indus.gateway.adapters._entra import ClientCredentials, TokenError

__all__ = [
    "CHANGE_TYPES",
    "DEFAULT_LIFETIME_MINUTES",
    "GRAPH_URL",
    "RENEW_AT",
    "Held",
    "SubscriptionKeeper",
    "SubscriptionSpec",
    "parse_graph_time",
]

log = logging.getLogger(__name__)

GRAPH_URL = "https://graph.microsoft.com/v1.0"
CHANGE_TYPES = frozenset({"created", "updated", "deleted"})
DEFAULT_LIFETIME_MINUTES = 60
MAX_LIFETIME_MINUTES = 41_760  # the longest any Graph resource allows (29 days)
RENEW_AT = 0.8
RETRY_FIRST_S = 30.0
RETRY_MAX_S = 900.0
# Never wait closer than this to an expiry before trying again.
EXPIRY_GUARD_S = 60.0
REQUEST_TIMEOUT_S = 30.0

_RESOURCE = re.compile(r"^[A-Za-z0-9/_().,'=$:@ -]{1,400}$")
_SUBSCRIPTION_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_FRACTION = re.compile(r"\.(\d{1,6})\d*")


def parse_graph_time(value: object) -> datetime | None:
    """A Graph timestamp (``2026-03-01T10:00:00.1234567Z``) as an aware datetime."""
    if not isinstance(value, str) or not value:
        return None
    text = _FRACTION.sub(lambda m: "." + m.group(1), value.strip()).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _graph_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class SubscriptionSpec:
    """One subscription the receiver keeps: ``platforms.msgraph.subscriptions[]``."""

    resource: str
    change_type: str = "created,updated"
    lifetime_minutes: int = DEFAULT_LIFETIME_MINUTES

    @property
    def key(self) -> str:
        return f"{self.change_type}|{self.resource}"

    @classmethod
    def read(cls, raw: object) -> SubscriptionSpec:
        """A spec from config; ``ValueError`` names what is wrong."""
        if not isinstance(raw, Mapping):
            raise ValueError("each subscription needs resource and change_type")
        entry: Mapping[str, Any] = raw  # pyright: ignore[reportUnknownVariableType]
        resource = str(entry.get("resource") or "").strip().strip("/")
        if not _RESOURCE.match(resource):
            raise ValueError(f"subscription resource {resource!r} is not a Graph resource path")
        kinds = [k.strip() for k in str(entry.get("change_type") or "created,updated").split(",")]
        if not kinds or any(k not in CHANGE_TYPES for k in kinds):
            raise ValueError("change_type must list created, updated and/or deleted")
        raw_life = entry.get("lifetime_minutes", DEFAULT_LIFETIME_MINUTES)
        if isinstance(raw_life, bool):
            raise ValueError("lifetime_minutes must be a whole number")
        try:
            lifetime = int(str(raw_life))
        except ValueError:
            raise ValueError("lifetime_minutes must be a whole number") from None
        if not 5 <= lifetime <= MAX_LIFETIME_MINUTES:
            raise ValueError(f"lifetime_minutes must be between 5 and {MAX_LIFETIME_MINUTES}")
        return cls(resource, ",".join(dict.fromkeys(kinds)), lifetime)


@dataclass(frozen=True)
class Held:
    """A live subscription: its id, when it was granted and when it expires."""

    subscription_id: str
    issued: datetime
    expires: datetime

    def renew_at(self) -> datetime:
        return self.issued + (self.expires - self.issued) * RENEW_AT


class _Gone(Exception):
    """Graph no longer has the subscription (404)."""


class SubscriptionKeeper:
    """Creates and renews the configured subscriptions (see the module notes)."""

    def __init__(
        self,
        *,
        specs: Sequence[SubscriptionSpec],
        credentials: ClientCredentials,
        client: Callable[[], httpx.AsyncClient],
        clock: Clock,
        notification_url: str,
        client_state: str,
        store_path: Path | None,
        graph_url: str = GRAPH_URL,
    ) -> None:
        if not notification_url.startswith("https://"):
            raise ValueError("the notification URL must be https")
        self.specs = tuple(specs)
        self.credentials = credentials
        self._client = client
        self.clock = clock
        self.notification_url = notification_url
        self._client_state = client_state
        self.store_path = store_path
        self.graph_url = graph_url.rstrip("/")
        self.held: dict[str, Held] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._wake: dict[str, asyncio.Event] = {}
        self._recreate: set[str] = set()
        self._renew_now: set[str] = set()

    def __repr__(self) -> str:
        return f"<SubscriptionKeeper {len(self.specs)} subscription(s)>"

    # -- lifecycle ---------------------------------------------------------------------------------

    async def start(self) -> None:
        if self._tasks:
            return
        if self.store_path is not None:
            stored = await run_blocking(_read, self.store_path, label="msgraph subscriptions")
            wanted = {spec.key for spec in self.specs}
            self.held = {k: v for k, v in stored.items() if k in wanted}
        for spec in self.specs:
            self._wake[spec.key] = asyncio.Event()
            task = asyncio.ensure_future(self._keep(spec))
            self._tasks.append(task)

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def owns(self, subscription_id: str) -> bool:
        return any(h.subscription_id == subscription_id for h in self.held.values())

    def lifecycle(self, subscription_id: str, event: str) -> None:
        """Graph's lifecycle notice for one of our subscriptions: renew or recreate at once."""
        for key, held in self.held.items():
            if held.subscription_id != subscription_id:
                continue
            if event == "subscriptionRemoved":
                self._recreate.add(key)
            elif event == "reauthorizationRequired":
                self._renew_now.add(key)
            if event in ("reauthorizationRequired", "subscriptionRemoved"):
                wake = self._wake.get(key)
                if wake is not None:
                    wake.set()
            return

    # -- the per-subscription loop -----------------------------------------------------------------

    async def _keep(self, spec: SubscriptionSpec) -> None:
        backoff = RETRY_FIRST_S
        while True:
            held = self.held.get(spec.key)
            now = self.clock.now()
            try:
                if held is None or held.expires <= now or spec.key in self._recreate:
                    self._recreate.discard(spec.key)
                    held = await self._create(spec)
                elif held.renew_at() <= now or spec.key in self._renew_now:
                    self._renew_now.discard(spec.key)
                    held = await self._renew(spec, held)
                else:
                    await self._pause(spec, (held.renew_at() - now).total_seconds())
                    continue
            except _Gone:
                log.info("msgraph: subscription for %s was gone; creating it again", spec.resource)
                self.held.pop(spec.key, None)
                await self._save()
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "msgraph: subscription for %s not kept up (%s); retrying in %.0f s",
                    spec.resource,
                    _why(exc),
                    backoff,
                )
                wait = backoff
                current = self.held.get(spec.key)
                if current is not None:
                    left = (current.expires - self.clock.now()).total_seconds() - EXPIRY_GUARD_S
                    wait = max(min(wait, left), 1.0)
                backoff = min(backoff * 2, RETRY_MAX_S)
                await self._pause(spec, wait)
                continue
            backoff = RETRY_FIRST_S
            self.held[spec.key] = held
            await self._save()

    async def _pause(self, spec: SubscriptionSpec, seconds: float) -> None:
        """Sleep on the clock, or less when a lifecycle event wakes this subscription."""
        wake = self._wake.setdefault(spec.key, asyncio.Event())
        if not wake.is_set():
            sleeper = asyncio.ensure_future(self.clock.sleep(max(seconds, 0.0)))
            waiter = asyncio.ensure_future(wake.wait())
            try:
                await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (sleeper, waiter):
                    task.cancel()
                await asyncio.gather(sleeper, waiter, return_exceptions=True)
        wake.clear()

    # -- Graph calls -------------------------------------------------------------------------------

    async def _call(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        client = self._client()
        answer: httpx.Response | None = None
        for fresh in (False, True):
            token = await self.credentials.token(client, fresh=fresh)
            answer = await client.request(
                method,
                f"{self.graph_url}{path}",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=REQUEST_TIMEOUT_S,
                follow_redirects=False,
            )
            if answer.status_code != 401:
                break
            self.credentials.invalidate()
        assert answer is not None
        if answer.status_code == 404 and method == "PATCH":
            raise _Gone()
        if answer.status_code >= 400:
            raise RuntimeError(f"Graph answered HTTP {answer.status_code}{_graph_code(answer)}")
        data: Any = answer.json() if answer.content else {}
        return data if isinstance(data, dict) else {}  # pyright: ignore[reportUnknownVariableType]

    async def _create(self, spec: SubscriptionSpec) -> Held:
        now = self.clock.now()
        asked = now + timedelta(minutes=spec.lifetime_minutes)
        data = await self._call(
            "POST",
            "/subscriptions",
            {
                "changeType": spec.change_type,
                "notificationUrl": self.notification_url,
                "lifecycleNotificationUrl": self.notification_url,
                "resource": spec.resource,
                "expirationDateTime": _graph_time(asked),
                "clientState": self._client_state,
                "latestSupportedTlsVersion": "v1_2",
            },
        )
        subscription_id = str(data.get("id") or "")
        if not _SUBSCRIPTION_ID.match(subscription_id):
            raise RuntimeError("Graph did not return a subscription id")
        expires = parse_graph_time(data.get("expirationDateTime")) or asked
        log.info("msgraph: subscribed to %s until %s", spec.resource, _graph_time(expires))
        return Held(subscription_id, now, expires)

    async def _renew(self, spec: SubscriptionSpec, held: Held) -> Held:
        now = self.clock.now()
        asked = now + timedelta(minutes=spec.lifetime_minutes)
        data = await self._call(
            "PATCH",
            f"/subscriptions/{held.subscription_id}",
            {"expirationDateTime": _graph_time(asked)},
        )
        expires = parse_graph_time(data.get("expirationDateTime")) or asked
        return Held(held.subscription_id, now, expires)

    # -- the store ---------------------------------------------------------------------------------

    async def _save(self) -> None:
        if self.store_path is None:
            return
        rows = {
            key: {
                "id": h.subscription_id,
                "issued": _graph_time(h.issued),
                "expires": _graph_time(h.expires),
            }
            for key, h in self.held.items()
        }
        try:
            await run_blocking(_write, self.store_path, rows, label="msgraph subscriptions")
        except Exception as exc:
            log.warning("msgraph: subscriptions not saved: %s", type(exc).__name__)


def _graph_code(answer: httpx.Response) -> str:
    try:
        data: Any = answer.json()
    except ValueError:
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    code = error.get("code") if isinstance(error, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return (
        f" ({code})" if isinstance(code, str) and re.match(r"^[A-Za-z0-9_.]{1,64}$", code) else ""
    )


def _why(exc: BaseException) -> str:
    if isinstance(exc, TokenError | RuntimeError):
        return str(exc)
    return type(exc).__name__


def _read(path: Path) -> dict[str, Held]:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("msgraph: stored subscriptions unreadable: %s", type(exc).__name__)
        return {}
    found: dict[str, Held] = {}
    if not isinstance(raw, dict):
        return found
    for key, row in raw.items():  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(key, str) or not isinstance(row, dict):
            continue
        entry: dict[str, Any] = row  # pyright: ignore[reportUnknownVariableType]
        sid = str(entry.get("id") or "")
        issued = parse_graph_time(entry.get("issued"))
        expires = parse_graph_time(entry.get("expires"))
        if _SUBSCRIPTION_ID.match(sid) and issued is not None and expires is not None:
            found[key] = Held(sid, issued, expires)
    return found


def _write(path: Path, rows: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(path, rows, mode=0o600)
