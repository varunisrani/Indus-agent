"""Profile routes and delivery identity under multiplexing (P24-03).

**Routes** (``gateway.profile_routes``): ``{name, platform, profile, bot?, guild?, chat?, thread?,
user?, enabled}``. A route matches when every discriminator it names matches (AND). The most
specific match wins by additive weights — user 16, thread 8, chat 4, guild 2, none 0 — and equal
weights keep config order. Each route is tied to one receiving bot, named by profile in ``bot``
(unset or ``default``: the default profile's bot) and is skipped for anything that arrived through
another bot, so a chat-id route written for the shared bot can never re-home a DM with a
secondary's own bot. A ``chat`` route also matches threads
under that chat (the thread's parent id). With no match the receiving bot's own profile runs the
message.

**Identity first.** ``ProfileRouter.identify`` settles, once and before any session key is built,
which bot received the message (``transport_profile``: the profile whose adapter registered in the
``AdapterTable`` delivered it) and which profile runs it (``runtime_profile``). A message from an
adapter nobody registered, or routed to a profile this gateway does not serve, is ``Unresolved``:
``drop_unresolved`` logs one warning per target and, when the platform has already acknowledged
the update, sends that target one "please resend" notice.

**Exactly two resolvers.** ``intake_adapter`` — the bot that received it; live provenance only.
``delivery_adapter`` — who answers: the receiving bot when known and connected; a known receiving
bot that is offline answers nothing (never another bot); with no receiving bot known (a restored
source whose sidecar row is gone) the unique owner of (platform, runtime profile) answers — that
profile's own bot, else the one live bot whose routes target it — and two or none means no
delivery, logged once per target. A profile with a bot of its own (on any platform, connected or
not) never borrows another bot; a profile with no bot and no route never borrows the default bot.

**Persistence.** ``ReceivingBots`` keeps the receiving bot next to each route key in a small JSON
sidecar (``<launch home>/gateway/receiving_bots.json``, 0600, at most ``MAX_BOT_ROWS`` rows, oldest
dropped), so delivery after a restart goes through the same bot. Session keys use the runtime
profile's namespace (``core.session_keys``, format unchanged).
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from indus.foundation.errors import InvalidProfileId
from indus.foundation.fsio import atomic_write_text
from indus.foundation.profiles import DEFAULT_PROFILE, normalize_id
from indus.gateway.core._shapes import SourceLike, text_of
from indus.gateway.core.session_keys import build_session_key, profile_of_key

__all__ = [
    "MAX_BOT_ROWS",
    "RESEND_TEXT",
    "WEIGHTS",
    "AdapterTable",
    "ProfileRoute",
    "ProfileRouter",
    "ReceivingBots",
    "RouteQuery",
    "RoutingIdentity",
    "Unresolved",
    "match_route",
    "parse_routes",
    "routes_from_config",
]

log = logging.getLogger(__name__)

WEIGHTS: Final = {"user": 16, "thread": 8, "chat": 4, "guild": 2}
MAX_BOT_ROWS: Final = 20_000
BOTS_FILE: Final = "receiving_bots.json"
RESEND_TEXT: Final = (
    "This message could not be handed to the right profile, so it was not answered. "
    "Please send it again in a moment."
)
_ID_FIELDS: Final = ("guild", "chat", "thread", "user")


def _bot_key(name: object) -> str:
    """The receiving bot's profile: ``default`` for unset."""
    text = str(name).strip() if isinstance(name, str) else ""
    return text or DEFAULT_PROFILE


# -- routes ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteQuery:
    """What a route is matched against: the platform, the receiving bot and the ids."""

    platform: str
    bot: str = DEFAULT_PROFILE
    guild: str | None = None
    chat: str | None = None
    thread: str | None = None
    parent_chat: str | None = None
    user: str | None = None

    @classmethod
    def from_source(cls, source: SourceLike, *, bot: str) -> RouteQuery:
        def opt(value: object) -> str | None:
            return text_of(value) or None

        return cls(
            platform=text_of(source.platform).lower(),
            bot=_bot_key(bot),
            guild=opt(getattr(source, "guild_id", None)),
            chat=opt(source.chat_id),
            thread=opt(source.thread_id),
            parent_chat=opt(source.parent_chat_id),
            user=opt(source.user_id),
        )


@dataclass(frozen=True)
class ProfileRoute:
    """One routing rule (see the module notes)."""

    name: str
    platform: str
    profile: str
    bot: str = DEFAULT_PROFILE
    guild: str | None = None
    chat: str | None = None
    thread: str | None = None
    user: str | None = None
    enabled: bool = True

    @property
    def specificity(self) -> int:
        return sum(weight for key, weight in WEIGHTS.items() if getattr(self, key))

    def matches(self, query: RouteQuery) -> bool:
        if not self.enabled or self.platform != query.platform or self.bot != query.bot:
            return False
        if self.user is not None and self.user != query.user:
            return False
        if self.thread is not None and self.thread != query.thread:
            return False
        if self.chat is not None and self.chat not in (query.chat, query.parent_chat):
            return False
        return self.guild is None or self.guild == query.guild


def _route_id(value: object, field: str, name: str, problems: list[str]) -> tuple[bool, str | None]:
    """``(ok, id)``: strings as given, integers as text; anything else is a problem."""
    if value is None:
        return True, None
    if isinstance(value, bool) or not isinstance(value, str | int):
        problems.append(
            f"route {name!r}: {field} must be text or a whole number (quote it in the YAML)"
        )
        return False, None
    text = str(value).strip()
    if not text:
        problems.append(f"route {name!r}: {field} is empty")
        return False, None
    return True, text


def parse_routes(raw: object) -> tuple[list[ProfileRoute], list[str]]:
    """Routes from config, most specific first (ties keep config order), and the problems found.
    A row with a problem is skipped whole (a half-read route could match too much)."""
    problems: list[str] = []
    routes: list[ProfileRoute] = []
    if raw is None:
        return routes, problems
    if not isinstance(raw, list):
        return routes, ["profile_routes must be a list"]
    for index, item in enumerate(cast(list[object], raw)):
        if not isinstance(item, Mapping):
            problems.append(f"route #{index + 1} is not a mapping")
            continue
        entry = cast(Mapping[str, object], item)
        name = str(entry.get("name") or f"#{index + 1}")
        platform = str(entry.get("platform") or "").strip().lower()
        if not platform or not entry.get("profile"):
            problems.append(f"route {name!r}: platform and profile are required")
            continue
        try:
            profile = normalize_id(entry.get("profile"))
            bot_raw = entry.get("bot")
            bot = DEFAULT_PROFILE if bot_raw in (None, "") else normalize_id(bot_raw)
        except InvalidProfileId:
            problems.append(f"route {name!r}: not a valid profile id")
            continue
        ids: dict[str, str | None] = {}
        ok = True
        for field in _ID_FIELDS:
            good, value = _route_id(entry.get(field), field, name, problems)
            ok = ok and good
            ids[field] = value
        if not ok:
            continue
        enabled = entry.get("enabled", True)
        routes.append(
            ProfileRoute(
                name=name,
                platform=platform,
                profile=profile,
                bot=bot,
                guild=ids["guild"],
                chat=ids["chat"],
                thread=ids["thread"],
                user=ids["user"],
                enabled=enabled is not False,
            )
        )
    routes.sort(key=lambda r: r.specificity, reverse=True)
    for problem in problems:
        log.warning("gateway.profile_routes: %s", problem)
    return routes, problems


def match_route(routes: Sequence[ProfileRoute], query: RouteQuery) -> ProfileRoute | None:
    """The first (most specific) route that matches ``query``."""
    return next((route for route in routes if route.matches(query)), None)


# -- adapters -------------------------------------------------------------------------------------


class AdapterTable:
    """The live adapters: one per (profile, platform). Thread-safe.

    A profile that ever registered a bot of its own *claims* that platform until it is released
    (unserved): its bot going offline does not make it a bot-less profile that could borrow
    another profile's bot."""

    def __init__(self) -> None:
        self._by_profile: dict[str, dict[str, object]] = {}
        self._claims: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def register(self, profile: str, platform: str, adapter: object) -> None:
        with self._lock:
            self._by_profile.setdefault(profile, {})[platform.lower()] = adapter
            self._claims.setdefault(profile, set()).add(platform.lower())

    def unregister(
        self, profile: str, platform: str | None = None, *, release: bool = False
    ) -> None:
        """The adapter went away (``platform`` None: all of the profile's). ``release`` also drops
        the claims (the profile is no longer served)."""
        with self._lock:
            if platform is None:
                self._by_profile.pop(profile, None)
                if release:
                    self._claims.pop(profile, None)
                return
            adapters = self._by_profile.get(profile)
            if adapters is not None:
                adapters.pop(platform.lower(), None)
            if release:
                self._claims.get(profile, set()).discard(platform.lower())

    def claims(self, profile: str) -> set[str]:
        """Platforms ``profile`` has a bot of its own on (connected or not)."""
        with self._lock:
            return set(self._claims.get(profile, set()))

    def adapter(self, profile: str, platform: str) -> object | None:
        with self._lock:
            return self._by_profile.get(profile, {}).get(platform.lower())

    def for_profile(self, profile: str) -> dict[str, object]:
        """The profile's own adapters (empty for a profile with none: never another's)."""
        with self._lock:
            return dict(self._by_profile.get(profile, {}))

    def owner_of(self, adapter: object, platform: str) -> str | None:
        """The profile whose registered adapter for ``platform`` is ``adapter``."""
        with self._lock:
            for profile, adapters in self._by_profile.items():
                if adapters.get(platform.lower()) is adapter:
                    return profile
        return None


# -- identity -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutingIdentity:
    """Which bot received a message (None: unknown) and which profile runs it."""

    platform: str
    runtime_profile: str
    transport_profile: str | None
    live: bool = True
    route: str | None = None


@dataclass(frozen=True)
class Unresolved:
    """A message whose identity could not be settled; it is dropped."""

    reason: str
    profile: str | None = None


class ReceivingBots:
    """Session key → receiving bot, persisted beside the routing index (see the module notes)."""

    def __init__(self, home: Path, *, limit: int = MAX_BOT_ROWS) -> None:
        self.path = home / "gateway" / BOTS_FILE
        self.limit = limit
        self._rows: dict[str, str] | None = None
        self._lock = threading.Lock()

    def _load(self) -> dict[str, str]:
        if self._rows is not None:
            return self._rows
        rows: dict[str, str] = {}
        try:
            data: object = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            for key, value in cast(dict[object, object], data).items():
                if isinstance(key, str) and isinstance(value, str):
                    try:
                        rows[key] = normalize_id(value)
                    except InvalidProfileId:
                        continue
        self._rows = rows
        return rows

    def _save(self, rows: Mapping[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_text(self.path, json.dumps(dict(rows)), mode=0o600)

    def get(self, session_key: str) -> str | None:
        with self._lock:
            return self._load().get(session_key)

    def remember(self, session_key: str, bot: str) -> None:
        with self._lock:
            rows = self._load()
            if rows.get(session_key) == bot:
                return
            rows.pop(session_key, None)
            rows[session_key] = bot
            while len(rows) > self.limit:
                rows.pop(next(iter(rows)))
            try:
                self._save(rows)
            except OSError as exc:
                log.warning("the receiving bot could not be saved: %s", exc.strerror)

    def forget(self, session_key: str) -> None:
        with self._lock:
            rows = self._load()
            if rows.pop(session_key, None) is not None:
                try:
                    self._save(rows)
                except OSError as exc:
                    log.warning("the receiving bot could not be saved: %s", exc.strerror)


type Notify = Callable[[SourceLike, str], object]


class ProfileRouter:
    """Identity, the two adapter resolvers and the drop rule (see the module notes)."""

    def __init__(
        self,
        routes: Iterable[ProfileRoute],
        adapters: AdapterTable,
        *,
        served: Callable[[], Collection[str]],
        bots: ReceivingBots | None = None,
    ) -> None:
        self.routes = sorted(routes, key=lambda r: r.specificity, reverse=True)
        self.adapters = adapters
        self.served = served
        self.bots = bots
        self._warned: set[tuple[str, ...]] = set()
        self._resend_sent: set[tuple[str, ...]] = set()
        self._lock = threading.Lock()

    # -- identity -------------------------------------------------------------------------------

    def identify(self, source: SourceLike, adapter: object) -> RoutingIdentity | Unresolved:
        """Who received ``source`` (through ``adapter``) and which profile runs it."""
        platform = text_of(source.platform).lower()
        transport = self.adapters.owner_of(adapter, platform)
        if transport is None:
            return Unresolved("unknown_bot")
        route = match_route(self.routes, RouteQuery.from_source(source, bot=transport))
        runtime = route.profile if route is not None else transport
        try:
            served = runtime in self.served()
        except Exception:
            served = False
        if not served:
            return Unresolved("unserved_profile", runtime)
        with self._lock:
            self._warned.discard(_target(source))
            self._resend_sent.discard(_target(source))
        return RoutingIdentity(
            platform=platform,
            runtime_profile=runtime,
            transport_profile=transport,
            live=True,
            route=route.name if route is not None else None,
        )

    def restored_identity(self, session_key: str, platform: str) -> RoutingIdentity:
        """The identity of a session revived from durable state (no live provenance)."""
        bot = self.bots.get(session_key) if self.bots is not None else None
        return RoutingIdentity(
            platform=platform.lower(),
            runtime_profile=profile_of_key(session_key),
            transport_profile=bot,
            live=False,
        )

    def remember(self, session_key: str, identity: RoutingIdentity) -> None:
        """Persist the receiving bot beside the route key."""
        if self.bots is not None and identity.transport_profile is not None:
            self.bots.remember(session_key, identity.transport_profile)

    def session_key(self, source: SourceLike, identity: RoutingIdentity, **options: bool) -> str:
        """The session key in the runtime profile's namespace."""
        return build_session_key(source, profile=identity.runtime_profile, **options)

    # -- the two resolvers ----------------------------------------------------------------------

    def intake_adapter(self, identity: RoutingIdentity) -> object | None:
        """The adapter that received the message; None without live provenance."""
        if not identity.live or identity.transport_profile is None:
            return None
        return self.adapters.adapter(identity.transport_profile, identity.platform)

    def delivery_adapter(self, identity: RoutingIdentity) -> object | None:
        """The adapter that answers (see the module notes); None = deliver nowhere."""
        received_by = identity.transport_profile
        if received_by is not None:
            # A known receiving bot answers, or nobody does: never another bot by guess.
            return self.adapters.adapter(received_by, identity.platform)
        owners = self.owners(identity.platform, identity.runtime_profile)
        if len(owners) == 1:
            return self.adapters.adapter(owners[0], identity.platform)
        key = ("delivery", identity.platform, identity.runtime_profile)
        with self._lock:
            first = key not in self._warned
            self._warned.add(key)
        if first:
            log.warning(
                "no single bot can answer profile %s on %s (%d candidates); not delivered",
                identity.runtime_profile,
                identity.platform,
                len(owners),
            )
        return None

    def owners(self, platform: str, profile: str) -> list[str]:
        """Bots that may deliver for ``profile`` on ``platform``: its own; none when it has a bot
        of its own anywhere (offline or on another platform: its own credential boundary); else
        the live bots whose routes target it."""
        platform = platform.lower()
        if self.adapters.adapter(profile, platform) is not None:
            return [profile]
        if self.adapters.claims(profile):
            return []
        bots = {
            route.bot
            for route in self.routes
            if route.enabled and route.platform == platform and route.profile == profile
        }
        return sorted(b for b in bots if self.adapters.adapter(b, platform) is not None)

    # -- dropping -------------------------------------------------------------------------------

    def drop_unresolved(
        self,
        source: SourceLike,
        unresolved: Unresolved,
        *,
        acknowledged: bool,
        notify: Notify | None = None,
    ) -> bool:
        """Drop the message: one warning per target, and one "please resend" notice when the
        platform already acknowledged it. True when the notice was sent."""
        target = _target(source)
        with self._lock:
            first = target not in self._warned
            self._warned.add(target)
            resend = acknowledged and notify is not None and target not in self._resend_sent
            if resend:
                self._resend_sent.add(target)
        if first:
            log.warning(
                "dropped a %s message: %s%s",
                target[0],
                unresolved.reason,
                f" ({unresolved.profile})" if unresolved.profile else "",
            )
        if resend and notify is not None:
            try:
                notify(source, RESEND_TEXT)
            except Exception as exc:
                log.warning("the resend notice could not be sent: %s", type(exc).__name__)
                return False
            return True
        return False


def _target(source: SourceLike) -> tuple[str, ...]:
    return (
        text_of(source.platform).lower(),
        text_of(source.chat_id),
        text_of(source.thread_id),
    )


def routes_from_config(raw: Any) -> list[ProfileRoute]:
    """``parse_routes`` without the problem list (logged there)."""
    return parse_routes(raw)[0]
