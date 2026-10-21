"""The delivery target grammar, shared by cron (P14-13) and the gateway (P15-20).

A ``deliver`` value is a comma list of tokens:

- ``local`` — keep the output document only; an empty or missing value means ``local``;
- ``origin`` — back to the chat the job was created from (``local`` when it has none);
- ``all`` — every connected platform's home channel, worked out by the gateway at send time;
- ``<platform>`` — that platform's home channel (``chat_id`` None);
- ``<platform>:<chat_id>[:<thread_id>]`` — one chat, optionally one thread or topic.

Reserved words and platform names are matched without case; chat and thread ids keep theirs.
Tokens are trimmed, empty pieces are dropped, and duplicates collapse on (platform, chat, thread)
keeping the first spelling and the order. A platform name that is not known is reported as an
``unknown_platform`` problem and never turned into ``local``.

Known platforms are the gateway adapters' names plus whatever ``register_platform`` adds (plugin
platforms) and every platform that has a deliverer (``cron.delivery.register_deliverer``). A
platform whose chat ids contain ``:`` registers its own address splitter; the default splits the
part after the platform at its first ``:`` into chat and thread.

Nothing here touches the network or the config: it is pure parsing, so the gateway imports it
unchanged.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "ALL",
    "KNOWN_PLATFORMS",
    "LOCAL",
    "ORIGIN",
    "DeliveryTarget",
    "ParsedTargets",
    "TargetProblem",
    "first_concrete",
    "is_known_platform",
    "join_tokens",
    "parse_targets",
    "pin_origin",
    "register_platform",
    "split_tokens",
]

LOCAL = "local"
ORIGIN = "origin"
ALL = "all"
RESERVED = frozenset({LOCAL, ORIGIN, ALL})

# The gateway adapters planned for P15/P16, by the name their deliverer registers under.
KNOWN_PLATFORMS: frozenset[str] = frozenset(
    {
        "bluebubbles",
        "buzz",
        "dingtalk",
        "discord",
        "email",
        "feishu",
        "google_chat",
        "homeassistant",
        "irc",
        "line",
        "matrix",
        "mattermost",
        "ntfy",
        "photon",
        "qqbot",
        "raft",
        "self-chat",  # P24-07: the profile's own canonical bot chat (gateway.rooms.peers)
        "signal",
        "simplex",
        "slack",
        "sms",
        "teams",
        "telegram",
        "wecom",
        "wecom_callback",
        "weixin",
        "whatsapp",
        "whatsapp_cloud",
        "yuanbao",
    }
)

type Splitter = Callable[[str], tuple[str, str | None]]
type ProblemCode = Literal["unknown_platform", "bad_address"]

_lock = threading.Lock()
_extra_platforms: dict[str, Splitter | None] = {}
_live_platforms: Callable[[], Iterable[str]] | None = None


@dataclass(frozen=True)
class DeliveryTarget:
    """One place to deliver to. ``platform`` is a platform name or ``local``/``origin``/``all``.

    ``is_origin`` marks the ``origin`` token (resolved per job at send time); ``is_explicit``
    marks a written ``platform:chat`` address (as opposed to a home channel or a broadcast).
    ``raw`` is the token as written.
    """

    platform: str
    chat_id: str | None = None
    thread_id: str | None = None
    is_origin: bool = False
    is_explicit: bool = False
    raw: str = ""

    @property
    def key(self) -> tuple[str, str | None, str | None]:
        return (self.platform, self.chat_id, self.thread_id)

    @property
    def is_local(self) -> bool:
        return self.platform == LOCAL

    @property
    def is_broadcast(self) -> bool:
        return self.platform == ALL

    @property
    def is_concrete(self) -> bool:
        """A written chat address on a real platform."""
        return self.platform not in RESERVED and self.chat_id is not None

    def render(self) -> str:
        """The canonical token for this target."""
        if self.platform in RESERVED or self.chat_id is None:
            return self.platform
        if self.thread_id is None:
            return f"{self.platform}:{self.chat_id}"
        return f"{self.platform}:{self.chat_id}:{self.thread_id}"


@dataclass(frozen=True)
class TargetProblem:
    token: str
    code: ProblemCode
    message: str


@dataclass(frozen=True)
class ParsedTargets:
    targets: tuple[DeliveryTarget, ...]
    problems: tuple[TargetProblem, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def render(self) -> str:
        return join_tokens(t.render() for t in self.targets)


# -- platform names -------------------------------------------------------------------------------


def register_platform(name: str, *, splitter: Splitter | None = None) -> Callable[[], None]:
    """Make ``name`` a known platform (plugins); ``splitter`` parses its ``chat[:thread]`` part.

    Returns the undo.
    """
    key = name.strip().lower()
    if not key or key in RESERVED or ":" in key or "," in key:
        raise ValueError(f"not a usable platform name: {name!r}")
    with _lock:
        _extra_platforms[key] = splitter

    def undo() -> None:
        with _lock:
            if _extra_platforms.get(key) is splitter:
                _extra_platforms.pop(key, None)

    return undo


def set_live_platforms(source: Callable[[], Iterable[str]] | None) -> None:
    """Platforms that count as known because something can deliver to them right now (the
    delivery module points this at its deliverer registry)."""
    global _live_platforms
    _live_platforms = source


def is_known_platform(name: str) -> bool:
    key = name.strip().lower()
    if key in KNOWN_PLATFORMS:
        return True
    with _lock:
        if key in _extra_platforms:
            return True
    live = _live_platforms
    if live is not None:
        try:
            return key in {str(p).lower() for p in live()}
        except Exception:
            return False
    return False


def _splitter_for(platform: str) -> Splitter:
    with _lock:
        custom = _extra_platforms.get(platform)
    return custom if custom is not None else _default_split


def _default_split(address: str) -> tuple[str, str | None]:
    chat, sep, thread = address.partition(":")
    return chat, (thread if sep else None)


# -- parsing --------------------------------------------------------------------------------------


def split_tokens(value: str | Sequence[str] | None) -> list[str]:
    """The raw tokens of a ``deliver`` value: comma split, trimmed, empty pieces dropped.

    A list (hand-edited job file, MCP client) is flattened, each item split on commas too.
    """
    if value is None:
        return []
    items = [value] if isinstance(value, str) else [str(v) for v in value]
    out: list[str] = []
    for item in items:
        out.extend(piece.strip() for piece in item.split(",") if piece.strip())
    return out


def join_tokens(tokens: Iterable[str]) -> str:
    return ",".join(tokens)


def _parse_one(token: str) -> DeliveryTarget | TargetProblem:
    lowered = token.lower()
    if lowered in RESERVED:
        return DeliveryTarget(lowered, is_origin=lowered == ORIGIN, raw=token)
    platform, sep, address = token.partition(":")
    platform = platform.strip().lower()
    if not platform:
        return TargetProblem(token, "bad_address", f"{token!r} has no platform before ':'")
    if platform in RESERVED:
        return TargetProblem(token, "bad_address", f"{platform!r} takes no address")
    if not is_known_platform(platform):
        return TargetProblem(
            token, "unknown_platform", f"{platform!r} is not a platform Indus can deliver to"
        )
    if not sep:
        return DeliveryTarget(platform, raw=token)
    try:
        chat, thread = _splitter_for(platform)(address.strip())
    except Exception as exc:
        return TargetProblem(token, "bad_address", f"{token!r}: {exc}")
    chat = chat.strip()
    thread = thread.strip() if thread is not None else None
    if not chat:
        return TargetProblem(token, "bad_address", f"{token!r} has no chat id after ':'")
    return DeliveryTarget(platform, chat, thread or None, is_explicit=True, raw=token)


def parse_targets(value: str | Sequence[str] | None) -> ParsedTargets:
    """Parse a ``deliver`` value (see the module notes). Empty → ``local``."""
    tokens = split_tokens(value)
    if not tokens:
        return ParsedTargets((DeliveryTarget(LOCAL, raw=LOCAL),))
    seen: set[tuple[str, str | None, str | None]] = set()
    targets: list[DeliveryTarget] = []
    problems: list[TargetProblem] = []
    for token in tokens:
        parsed = _parse_one(token)
        if isinstance(parsed, TargetProblem):
            problems.append(parsed)
            continue
        if parsed.key in seen:
            continue
        seen.add(parsed.key)
        targets.append(parsed)
    return ParsedTargets(tuple(targets), tuple(problems))


def origin_target(origin: dict[str, str] | None) -> DeliveryTarget | None:
    """A stored origin (``{platform, chat_id, thread_id?}``) as a concrete target."""
    if not origin:
        return None
    platform = origin.get("platform", "").strip().lower()
    chat = origin.get("chat_id", "").strip()
    if not platform or not chat or platform in RESERVED:
        return None
    thread = origin.get("thread_id")
    return DeliveryTarget(
        platform, chat, thread or None, is_origin=True, raw=f"{ORIGIN}->{platform}"
    )


def first_concrete(
    value: str | Sequence[str] | None, origin: dict[str, str] | None = None
) -> DeliveryTarget | None:
    """The first ``platform:chat`` a job delivers to, its ``origin`` resolved; None if none."""
    for target in parse_targets(value).targets:
        if target.is_origin:
            resolved = origin_target(origin)
            if resolved is not None:
                return DeliveryTarget(resolved.platform, resolved.chat_id, resolved.thread_id)
            continue
        if target.is_concrete:
            return target
    return None


def pin_origin(value: str | Sequence[str] | None, parent: DeliveryTarget | None) -> str:
    """``value`` with ``origin`` replaced by ``parent``'s address (or ``local``).

    For jobs created from inside a cron run: the creating session is gone by the time the new
    job fires, so ``origin`` must not dangle. A missing value counts as ``origin`` there.
    """
    tokens = split_tokens(value) or [ORIGIN]
    replacement = parent.render() if parent is not None and parent.is_concrete else LOCAL
    pinned = [replacement if t.lower() == ORIGIN else t for t in tokens]
    return join_tokens(dict.fromkeys(pinned))
