"""The profile a dashboard request acts on (P18-10, R09.20).

One backend serves every profile under the machine's root. A request names its profile with
``?profile=<name>``; ``ProfileScopes.resolve`` turns that into a full ``Scope`` — the profile's
home, its ``.env``-aware env view, its config view and its secret store — built the same way the
process scope is (``foundation.bootstrap.attach_profile_state``). Nothing is created by a lookup:
an unknown, invalid or deleted profile is ``ProfileUnknown`` (404 at the HTTP layer), never a new
directory (R04.25).

Two FastAPI dependencies are the only way a route gets its scope (route modules never read the
home or the process env themselves, architecture rule 4):

- ``read_scope``: the named profile, else the backend's own;
- ``write_scope``: the same, except that on a host with more than one profile a request that names
  none is refused with 400 ``profile_required`` — an unnamed write would land on whichever profile
  the backend happened to start with. Every write or destructive route depends on it;
  ``unscoped_write_routes(app)`` lists the ones that do not (the route-introspection check).

``install(app, scopes)`` puts the resolver on ``app.state``; WebSocket routes call
``ProfileScopes.resolve`` themselves (a socket cannot answer 404).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from starlette.requests import HTTPConnection

from indus.foundation.bootstrap import attach_profile_state
from indus.foundation.envload import build_env
from indus.foundation.errors import InvalidProfileId
from indus.foundation.home import HOME_ENV
from indus.foundation.profiles import (
    is_live,
    list_profiles,
    normalize_id,
    profile_home,
    profile_id_of,
)
from indus.foundation.scope import Scope

__all__ = [
    "PROFILE_PARAM",
    "STATE_KEY",
    "WRITE_METHODS",
    "ProfileRequired",
    "ProfileScopes",
    "ProfileUnknown",
    "build_profile_scope",
    "install",
    "read_scope",
    "scopes_of",
    "unscoped_write_routes",
    "write_scope",
]

PROFILE_PARAM: Final = "profile"
STATE_KEY: Final = "profile_scopes"
WRITE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class ProfileUnknown(LookupError):
    """No live profile has this name (or the name is not a profile id)."""

    def __init__(self, name: str | None) -> None:
        super().__init__("no such profile")
        self.name = name


class ProfileRequired(ValueError):
    """A write on a multi-profile host named no profile."""

    def __init__(self) -> None:
        super().__init__("this backend serves several profiles: name the profile to change")


def build_profile_scope(base: Scope, home: Path) -> Scope:
    """A full scope for another profile at ``home``: the backend's inherited process env with
    ``INDUS_HOME`` pinned there, that profile's ``.env``, config and secret store."""
    inherited = dict(base.env.base)
    inherited[HOME_ENV] = str(home)
    draft = Scope(
        profile_id=profile_id_of(home),
        home=home,
        root=base.root,
        env=build_env(home, inherited),
        clock=base.clock,
        routed=True,
    )
    return attach_profile_state(draft, inherited)


def _same_dir(a: Path, b: Path) -> bool:
    if a == b:
        return True
    try:
        return a.resolve(strict=False) == b.resolve(strict=False)
    except OSError:
        return False


class ProfileScopes:
    """Resolves ``?profile=`` names to scopes for one backend whose own scope is ``base``."""

    def __init__(
        self,
        base: Scope,
        *,
        build: Callable[[Scope, Path], Scope] = build_profile_scope,
    ) -> None:
        self.base = base
        self._build = build

    def profiles(self) -> list[str]:
        """The live profiles under the root, ``default`` first."""
        return list_profiles(self.base.root)

    def is_multi(self) -> bool:
        """More than one profile lives under the root (read now, not at start)."""
        return len(self.profiles()) > 1

    def resolve(self, name: str | None) -> Scope:
        """The scope ``name`` refers to (blank: the backend's own). ``ProfileUnknown`` otherwise."""
        text = (name or "").strip()
        if not text:
            return self.base
        try:
            profile = normalize_id(text)
        except InvalidProfileId:
            raise ProfileUnknown(None) from None
        try:
            live = is_live(self.base.root, profile)
        except (InvalidProfileId, OSError):
            live = False
        if not live:
            raise ProfileUnknown(profile)
        home = profile_home(self.base.root, profile)
        if _same_dir(home, self.base.home):
            return self.base
        return self._build(self.base, home)

    def resolve_for_write(self, name: str | None) -> Scope:
        """``resolve``, refusing an unnamed write on a multi-profile host."""
        if not (name or "").strip() and self.is_multi():
            raise ProfileRequired()
        return self.resolve(name)


def install(app: FastAPI, scopes: ProfileScopes) -> None:
    """Make ``scopes`` the resolver of ``app``'s requests."""
    setattr(app.state, STATE_KEY, scopes)


def scopes_of(connection: HTTPConnection) -> ProfileScopes:
    found: object = getattr(connection.app.state, STATE_KEY, None)
    if not isinstance(found, ProfileScopes):
        raise RuntimeError("the dashboard app has no profile resolver installed")
    return found


def _named(connection: HTTPConnection) -> str | None:
    return connection.query_params.get(PROFILE_PARAM)


def _not_found(exc: ProfileUnknown) -> HTTPException:
    detail: dict[str, Any] = {
        "error": "profile_not_found",
        "code": "profile_not_found",
        "message": "No such profile.",
    }
    if exc.name is not None:
        detail["profile"] = exc.name
    return HTTPException(status_code=404, detail=detail)


def read_scope(request: HTTPConnection) -> Scope:
    """Dependency: the scope of a read (the backend's own profile unless one is named)."""
    try:
        return scopes_of(request).resolve(_named(request))
    except ProfileUnknown as exc:
        raise _not_found(exc) from None


def write_scope(request: HTTPConnection) -> Scope:
    """Dependency: the scope of a write or destructive call (see the module notes)."""
    try:
        return scopes_of(request).resolve_for_write(_named(request))
    except ProfileRequired:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "profile_required",
                "code": "profile_required",
                "message": "This backend serves several profiles: add ?profile=<name> to say "
                "which one to change.",
            },
        ) from None
    except ProfileUnknown as exc:
        raise _not_found(exc) from None


def _depends_on(dependant: Any, target: Callable[..., object], seen: set[int]) -> bool:
    if id(dependant) in seen:
        return False
    seen.add(id(dependant))
    if getattr(dependant, "call", None) is target:
        return True
    children: Iterable[Any] = getattr(dependant, "dependencies", ()) or ()
    return any(_depends_on(child, target, seen) for child in children)


def _scoped_by_include(dependencies: Iterable[Any]) -> bool:
    return any(getattr(dep, "dependency", None) is write_scope for dep in dependencies)


def unscoped_write_routes(app: FastAPI, *, methods: Iterable[str] = WRITE_METHODS) -> list[str]:
    """``"METHOD /path"`` of every write route of ``app`` that does not depend on
    ``write_scope`` (empty when every write route is scoped). Routers added with
    ``include_router`` (FastAPI keeps them as one wrapper route around the original router) are
    walked too, with their prefix and their include-level dependencies."""
    wanted = {m.upper() for m in methods}
    missing: list[str] = []

    def walk(routes: Iterable[Any], prefix: str, inherited: bool) -> None:
        for route in routes:
            original = getattr(route, "original_router", None)
            if original is not None:
                context = getattr(route, "include_context", None)
                walk(
                    getattr(original, "routes", ()),
                    prefix + str(getattr(context, "prefix", "") or ""),
                    inherited or _scoped_by_include(getattr(context, "dependencies", ()) or ()),
                )
                continue
            if not isinstance(route, APIRoute):
                continue
            writes = sorted(wanted & {m.upper() for m in (route.methods or set())})
            if not writes or inherited:
                continue
            if _depends_on(route.dependant, write_scope, set()):
                continue
            missing.extend(f"{method} {prefix}{route.path}" for method in writes)

    walk(app.routes, "", False)
    return missing


def query_profile(params: Mapping[str, str]) -> str | None:
    """The ``profile`` named in query ``params`` (for WebSocket routes)."""
    return params.get(PROFILE_PARAM)
