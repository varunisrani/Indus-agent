"""Shared ingress: secondary profiles' HTTP apps on the default profile's listener (P24-04).

Under multiplexing the default profile owns each HTTP listener (API server, webhook receiver,
callback adapters). A secondary profile's port-binding app does not bind a port of its own: it is
mounted on the default listener with ``SharedIngress.mount`` and reached at ``/p/<profile>/<path>``.

A request, in order:

- a path outside ``/p/`` goes to the default profile's app, untouched;
- ``/p/<profile>/<tail>``: ``<profile>`` must be a profile id in canonical form and not
  ``default`` (the default profile is served un-prefixed), and one of that profile's mounts must
  claim ``<tail>`` (a mount claims exact paths, or prefixes written with a trailing ``/``);
  otherwise the answer is 404 (a WebSocket is closed) — never the default profile's handler;
- the claiming app gets the request with the prefix taken off ``path`` / ``raw_path`` and added to
  ``root_path``, the query string kept, and runs inside the mounted profile's ``Scope``.

Each mounted app is built from its own profile's scope, so it checks the request with that
profile's own API keys or webhook secrets: a request signed for profile B is accepted at
``/p/b/…`` and refused (by the default profile's own check) at the un-prefixed path.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from indus.foundation.profiles import DEFAULT_PROFILE, ID_PATTERN, RESERVED_IDS
from indus.foundation.scope import Scope, use_scope
from indus.gateway.http.serving import Asgi, Receive, Scope_, Send, send_json

__all__ = ["PREFIX", "IngressMount", "SharedIngress", "profile_url"]

log = logging.getLogger(__name__)

PREFIX: Final = "/p/"
_NOT_FOUND = {"error": "not_found", "detail": "no such profile endpoint"}
_WS_NOT_FOUND = 4404


@dataclass(frozen=True)
class IngressMount:
    """One profile app on the shared listener and the paths it claims."""

    profile: str
    name: str
    app: Asgi
    paths: tuple[str, ...]
    scope: Scope | None = None

    def claims(self, tail: str) -> bool:
        for path in self.paths:
            if path.endswith("/"):
                if tail.startswith(path) or tail == path.rstrip("/"):
                    return True
            elif tail == path:
                return True
        return False


def _profile_segment(segment: str) -> str | None:
    """The segment when it is a servable secondary profile id, written canonically."""
    if segment == DEFAULT_PROFILE or segment in RESERVED_IDS:
        return None
    return segment if ID_PATTERN.fullmatch(segment) else None


def profile_url(base: str, profile: str, path: str = "/") -> str:
    """Where ``profile``'s ``path`` is reached on the shared listener at ``base``."""
    return f"{base.rstrip('/')}{PREFIX}{profile}/{path.lstrip('/')}"


class SharedIngress:
    """The ASGI front of one default-profile listener (see the module notes)."""

    def __init__(self, default_app: Asgi) -> None:
        self.default_app = default_app
        self._mounts: dict[str, dict[str, IngressMount]] = {}

    def mount(
        self,
        profile: str,
        name: str,
        app: Asgi,
        *,
        paths: Iterable[str],
        scope: Scope | None = None,
    ) -> IngressMount:
        """Serve ``app`` for ``profile`` at ``/p/<profile>/<path>`` for each claimed path."""
        if _profile_segment(profile) is None:
            raise ValueError(f"{profile!r} cannot be mounted (default, reserved or not an id)")
        claimed = tuple(p if p.startswith("/") else f"/{p}" for p in paths)
        if not claimed:
            raise ValueError("a mount needs at least one path")
        found = IngressMount(profile, name, app, claimed, scope)
        self._mounts.setdefault(profile, {})[name] = found
        log.info("profile %s: %s mounted at %s%s/", profile, name, PREFIX, profile)
        return found

    def unmount(self, profile: str, name: str | None = None) -> None:
        """Remove one app of ``profile`` (``name``) or all of them; new requests get 404."""
        if name is None:
            self._mounts.pop(profile, None)
            return
        apps = self._mounts.get(profile)
        if apps is not None:
            apps.pop(name, None)
            if not apps:
                self._mounts.pop(profile, None)

    def mounted(self, profile: str) -> list[str]:
        return sorted(self._mounts.get(profile, {}))

    def _find(self, profile: str, tail: str) -> IngressMount | None:
        for found in list(self._mounts.get(profile, {}).values()):
            if found.claims(tail):
                return found
        return None

    async def __call__(self, scope: Scope_, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        path = scope.get("path", "")
        if kind not in ("http", "websocket") or not isinstance(path, str):
            await self.default_app(scope, receive, send)
            return
        if not (path == PREFIX.rstrip("/") or path.startswith(PREFIX)):
            await self.default_app(scope, receive, send)
            return
        segment, _, rest = path[len(PREFIX) :].partition("/")
        profile = _profile_segment(segment)
        tail = "/" + rest
        found = self._find(profile, tail) if profile is not None else None
        if found is None:
            await _not_found(kind, send)
            return
        prefix = f"{PREFIX}{segment}"
        child = dict(scope)
        child["path"] = tail
        raw = scope.get("raw_path")
        if isinstance(raw, bytes) and raw.startswith(prefix.encode()):
            child["raw_path"] = raw[len(prefix) :] or b"/"
        elif raw is not None:
            child["raw_path"] = tail.encode()
        child["root_path"] = f"{scope.get('root_path', '')}{prefix}"
        if found.scope is None:
            await found.app(child, receive, send)
            return
        with use_scope(found.scope):
            await found.app(child, receive, send)


async def _not_found(kind: object, send: Send) -> None:
    if kind == "websocket":
        await send({"type": "websocket.close", "code": _WS_NOT_FOUND})
        return
    await send_json(send, 404, _NOT_FOUND)
