"""The ``bots.*`` methods the desktop's Bots surface calls (P24-31).

Every profile is a bot. ``BotsRpc(profiles, chats, routines)`` answers, with plain JSON:

- ``bots.list {}`` → ``{bots: [{profile, avatar, hidden, sections, routines, canonical}]}`` where
  ``canonical`` is ``{session_id}`` of the bot's forever-chat or ``null`` when it has none yet;
- ``bots.open {profile}`` → ``{session_id, created}``: the forever-chat, created only when none
  exists;
- ``bots.update {profile, avatar?, hidden?, sections?}`` → the bot.

``profile_rows(rows)`` adds the same ``canonical_session`` to each ``profiles.list`` row (the
dashboard's ``GET /api/profiles``, ``indus.web.routes.profiles``), so the row's preview and a click
open one chat. A profile whose lookup fails (a broken or locked store) gets ``null`` there: the
profile list never fails for one bot.

The forever-chat's identity is only (profile, canonical title) — the ``CanonicalChats`` port. Its
real implementation is the state layer's canonical-chat lookup (P24-07: hidden rows included,
compression lineage followed to the live tip, the lookup run again inside the write before
creating — adopt before mint). Nothing here stores a session-id pointer or looks at recency. Opens
of one bot are serialised in this process as well, so two clicks that reach the daemon together
still make one chat.

Bot metadata (avatar, hidden flag, sections) is ``<profile home>/bot.json``, written atomically.
Avatars: ``{kind: "face", seed}`` (our geometric face, drawn by the client from the seed),
``{kind: "upload" | "portrait", data}`` (a ``data:image/png|jpeg|webp;base64,…`` URL of at most
``MAX_AVATAR_BYTES`` decoded bytes) or ``{kind: "pet"}`` (the profile's pet, P24-19).

Params are checked strictly (an unknown key is refused). The table is transport-free; mounting
``handlers()`` on the daemon dispatcher once the names are declared in ``indus.protocol`` is
wiring for the integrator (as for ``groups.*``).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from indus.foundation.errors import IndusError
from indus.foundation.fsio import atomic_write_json
from indus.foundation.profiles import list_profiles, profile_home
from indus.foundation.scope import Scope, make_scope
from indus.state import bot_chat, registry
from indus.state.connection import default_store_path
from indus.state.registry import StoreHandle

__all__ = [
    "AVATAR_KINDS",
    "BOT_FILE",
    "MAX_AVATAR_BYTES",
    "METHODS",
    "BadAvatar",
    "BotsRpc",
    "BotsRpcError",
    "CanonicalChats",
    "CronRoutines",
    "Profiles",
    "RootProfiles",
    "Routines",
    "StoreCanonicalChats",
    "UnknownBot",
    "for_root",
]

BOT_FILE: Final = "bot.json"
MAX_AVATAR_BYTES: Final = 256 * 1024
MAX_SECTIONS: Final = 20
AVATAR_KINDS: Final = ("face", "upload", "portrait", "pet")
_DATA_URL: Final = re.compile(r"^data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=\s]+)$")


class BotsRpcError(IndusError):
    """Bad params or an unknown method."""

    default_code = "bots.error"


class UnknownBot(IndusError):
    default_code = "bots.unknown_profile"


class BadAvatar(IndusError):
    default_code = "bots.bad_avatar"


class CanonicalChats(Protocol):
    """A profile's forever-chat, by (profile, canonical title) only."""

    def find(self, profile: str) -> str | None:
        """The live tip of the profile's canonical chat, or None (never creates)."""
        ...

    def ensure(self, profile: str) -> tuple[str, bool]:
        """The canonical chat's live tip and whether this call created it (adopt before mint)."""
        ...


class Profiles(Protocol):
    def names(self) -> Sequence[str]: ...

    def home(self, profile: str) -> Path: ...


class Routines(Protocol):
    def jobs(self, profile: str) -> Sequence[Mapping[str, Any]]:
        """The profile's cron jobs (``id``, ``name``, ``schedule``, ``enabled`` at least)."""
        ...


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Profile(_Params):
    profile: str = Field(min_length=1, max_length=128)


class _Avatar(_Params):
    kind: Literal["face", "upload", "portrait", "pet"]
    seed: str | None = Field(default=None, max_length=64)
    data: str | None = Field(default=None, max_length=MAX_AVATAR_BYTES * 2)


class _Update(_Profile):
    avatar: _Avatar | None = None
    hidden: bool | None = None
    sections: list[str] | None = None


def _check_avatar(avatar: _Avatar) -> dict[str, Any]:
    if avatar.kind == "pet":
        return {"kind": "pet"}
    if avatar.kind == "face":
        seed = (avatar.seed or "").strip()
        if not seed:
            raise BadAvatar("a generated face needs a seed")
        return {"kind": "face", "seed": seed}
    match = _DATA_URL.match(avatar.data or "")
    if match is None:
        raise BadAvatar("an avatar image must be a PNG, JPEG or WebP data URL")
    try:
        raw = base64.b64decode(re.sub(r"\s+", "", match.group(2)), validate=True)
    except (binascii.Error, ValueError):
        raise BadAvatar("the avatar image is not valid base64") from None
    if len(raw) > MAX_AVATAR_BYTES:
        raise BadAvatar(f"the avatar image is larger than {MAX_AVATAR_BYTES // 1024} KiB")
    return {"kind": avatar.kind, "data": avatar.data}


def _clean_sections(raw: Sequence[str]) -> list[str]:
    seen: list[str] = []
    for item in raw:
        name = item.strip()[:64]
        if name and name not in seen:
            seen.append(name)
    return seen[:MAX_SECTIONS]


class BotsRpc:
    """The ``bots.*`` handler table over the profile list, canonical chats and cron jobs."""

    def __init__(self, profiles: Profiles, chats: CanonicalChats, routines: Routines) -> None:
        self.profiles = profiles
        self.chats = chats
        self.routines = routines
        self._guard = threading.Lock()
        self._opening: dict[str, threading.Lock] = {}
        self._table: dict[str, tuple[type[_Params], Callable[[Any], dict[str, Any]]]] = {
            "bots.list": (_Params, self._list),
            "bots.open": (_Profile, self._open),
            "bots.update": (_Update, self._update),
        }

    # -- dispatch --------------------------------------------------------------------------------

    def handlers(self) -> dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]]:
        return {name: (lambda params, n=name: self.call(n, params)) for name in self._table}

    def call(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        entry = self._table.get(method)
        if entry is None:
            raise BotsRpcError(f"unknown method {method!r}")
        model, handler = entry
        try:
            parsed = model.model_validate(dict(params or {}))
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ())) or "params"
            raise BotsRpcError(f"bad params at {where}: {first.get('msg', 'invalid')}") from None
        return handler(parsed)

    # -- metadata --------------------------------------------------------------------------------

    def _known(self, profile: str) -> str:
        if profile not in self.profiles.names():
            raise UnknownBot(f"no bot is called {profile!r}")
        return profile

    def _meta(self, profile: str) -> dict[str, Any]:
        path = self.profiles.home(profile) / BOT_FILE
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        data: dict[str, object] = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
        avatar = data.get("avatar")
        sections = data.get("sections")
        listed = cast(list[object], sections) if isinstance(sections, list) else []
        return {
            "avatar": avatar if isinstance(avatar, dict) else {"kind": "face", "seed": profile},
            "hidden": data.get("hidden") is True,
            "sections": [item for item in listed if isinstance(item, str)],
        }

    def _row(self, profile: str) -> dict[str, Any]:
        found = self.chats.find(profile)
        routines = [
            {
                "id": str(job.get("id", "")),
                "name": str(job.get("name", "")),
                "schedule": str(job.get("schedule", "")),
                "enabled": bool(job.get("enabled", True)),
            }
            for job in self.routines.jobs(profile)
        ]
        return {
            "profile": profile,
            **self._meta(profile),
            "routines": routines,
            "canonical": None if found is None else {"session_id": found},
        }

    # -- methods ---------------------------------------------------------------------------------

    def _list(self, _: _Params) -> dict[str, Any]:
        return {"bots": [self._row(name) for name in self.profiles.names()]}

    def _lock_for(self, profile: str) -> threading.Lock:
        with self._guard:
            return self._opening.setdefault(profile, threading.Lock())

    def _open(self, p: _Profile) -> dict[str, Any]:
        profile = self._known(p.profile)
        with self._lock_for(profile):
            session_id, created = self.chats.ensure(profile)
        return {"session_id": session_id, "created": created}

    def _update(self, p: _Update) -> dict[str, Any]:
        profile = self._known(p.profile)
        meta = self._meta(profile)
        if p.avatar is not None:
            meta["avatar"] = _check_avatar(p.avatar)
        if p.hidden is not None:
            meta["hidden"] = p.hidden
        if p.sections is not None:
            meta["sections"] = _clean_sections(p.sections)
        atomic_write_json(self.profiles.home(profile) / BOT_FILE, {"v": 1, **meta}, mode=0o600)
        return {"bot": self._row(profile)}

    # -- profiles.list ---------------------------------------------------------------------------

    def profile_rows(self, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """``profiles.list`` rows with each bot's ``canonical_session`` (or null) added."""
        out: list[dict[str, Any]] = []
        names = set(self.profiles.names())
        for row in rows:
            name = str(row.get("name", ""))
            try:
                found = self.chats.find(name) if name in names else None
            except Exception:  # one bad store shows no chat, never a failed list
                found = None
            out.append(
                {**row, "canonical_session": None if found is None else {"session_id": found}}
            )
        return out


METHODS: Final = ("bots.list", "bots.open", "bots.update")


# -- the real ports (the daemon's) ----------------------------------------------------------------


class RootProfiles:
    """``Profiles`` over the live profiles under one root (``default`` first)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def names(self) -> Sequence[str]:
        return list_profiles(self.root)

    def home(self, profile: str) -> Path:
        return profile_home(self.root, profile)


def _quiet_scope(profiles: Profiles, profile: str, like: Scope) -> Scope:
    """A scope for another profile's home with no environment (no secret is read through it)."""
    return make_scope(profiles.home(profile), env={}, clock=like.clock)


class StoreCanonicalChats:
    """``CanonicalChats`` over each profile's own store (``indus.state.bot_chat``): the title-only
    lookup with the lineage tip, and adopt-before-mint inside the write. A store is opened for the
    call and closed after it."""

    def __init__(
        self, profiles: Profiles, scope: Scope, *, open_store: Callable[[Scope], StoreHandle]
    ) -> None:
        self.profiles = profiles
        self.scope = scope
        self._open = open_store

    def _with[T](self, profile: str, fn: Callable[[StoreHandle], T]) -> T:
        store = self._open(_quiet_scope(self.profiles, profile, self.scope))
        try:
            return fn(store)
        finally:
            store.close()

    def find(self, profile: str) -> str | None:
        # a profile without a store has no chat; a lookup must not create the store
        if not default_store_path(_quiet_scope(self.profiles, profile, self.scope)).exists():
            return None
        return self._with(profile, bot_chat.find_canonical)

    def ensure(self, profile: str) -> tuple[str, bool]:
        chat = self._with(profile, bot_chat.ensure_canonical)
        return chat.session_id, chat.created


class CronRoutines:
    """``Routines``: each profile's cron jobs (read only)."""

    def __init__(self, profiles: Profiles, scope: Scope) -> None:
        self.profiles = profiles
        self.scope = scope

    def jobs(self, profile: str) -> Sequence[Mapping[str, Any]]:
        from indus.cron.store import CronStore

        try:
            jobs = CronStore(_quiet_scope(self.profiles, profile, self.scope)).load()
        except Exception:  # a broken jobs file shows no routines, never a failed list
            return []
        return [
            {
                "id": job.id,
                "name": job.name or "",
                "schedule": job.schedule_display,
                "enabled": job.enabled,
            }
            for job in jobs
        ]


def for_root(scope: Scope) -> BotsRpc:
    """The daemon's table: every profile under ``scope.root``, their stores and cron jobs."""
    profiles = RootProfiles(scope.root)
    chats = StoreCanonicalChats(profiles, scope, open_store=registry.open_unshared)
    return BotsRpc(profiles, chats, CronRoutines(profiles, scope))
