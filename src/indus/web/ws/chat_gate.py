"""What the Chat tab's WebSocket routes share (P18-09 over the P18-06 helper).

Every chat endpoint is its handler wrapped by ``indus.web.ws.guarded_ws(purpose,
allow_internal=...)``: the helper runs the pre-accept checks in order (feature enabled → 4404,
credential → 4401, Host/Origin → 4403, peer → 4408) and calls the handler only with the
``WsGrant`` the credential proved. The grant is the only authority a route has about who is
calling — nothing in a frame or a query can claim one. ``grant.credential`` says which kind proved
it: ``token`` (the loopback session token), ``ticket`` (a single-use ticket minted over a logged-in
session) or ``internal`` (the process-lifetime credential of a client the server itself spawned —
the TUI child publishing its events; never handed to a browser).

The routes and what they take:

- ``/api/pty`` — ``web-pty``, a person's credential only;
- ``/api/events`` — ``web-events``, a person's credential only;
- ``/api/pub`` — ``web-pub``, the internal credential only (the handler checks the grant again);
- ``/api/ws`` — ``web-rpc``, a person's credential or the internal one (privileged methods are
  refused to the internal one).

``scope_of(scopes, grant)`` is the profile ``Scope`` a route serves: the grant's profile (a
gated ticket is bound to one), never a second reading of the query; ``ProfileUnknown`` when the
request named a profile that does not exist.

``SidecarLink`` is how the PTY route tells the TUI child where to publish: the loopback base URL
of this server (``ws://127.0.0.1:<port>``, no credential in it) and the internal credential, which
travels in its own environment variable and is offered as the ``indus-dashboard-internal.<value>``
subprotocol (``service.ws_auth.dashboard_offer``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import quote

from indus.foundation.scope import Scope
from indus.service.ws_auth import WEB_EVENTS, WEB_PTY, WEB_PUB, WEB_RPC
from indus.web.scope import ProfileScopes, ProfileUnknown
from indus.web.ws import WS_PURPOSE_ATTR, WsGrant

__all__ = [
    "INTERNAL",
    "PURPOSE_EVENTS",
    "PURPOSE_PTY",
    "PURPOSE_PUB",
    "PURPOSE_RPC",
    "SidecarLink",
    "WsGrant",
    "purpose_of",
    "scope_of",
]

PURPOSE_PTY: Final = WEB_PTY
PURPOSE_EVENTS: Final = WEB_EVENTS
PURPOSE_PUB: Final = WEB_PUB
PURPOSE_RPC: Final = WEB_RPC
INTERNAL: Final = "internal"


def purpose_of(endpoint: object) -> str | None:
    """The purpose ``guarded_ws`` tagged ``endpoint`` with (None for an unguarded one)."""
    found: object = getattr(endpoint, WS_PURPOSE_ATTR, None)
    return found if isinstance(found, str) else None


def scope_of(scopes: ProfileScopes, grant: WsGrant) -> Scope:
    """The scope ``grant`` serves (see the module notes)."""
    if grant.profile is None:
        raise ProfileUnknown(None)
    if grant.profile.home == scopes.base.home:
        return scopes.base
    return scopes.resolve(grant.profile.name)


@dataclass(frozen=True)
class SidecarLink:
    """Where the TUI child publishes its events (see the module notes)."""

    base_url: Callable[[], str | None]
    credential: Callable[[], str] = field(repr=False)

    def publish_url(self, channel: str) -> str | None:
        base = self.base_url()
        if not base:
            return None
        return f"{base.rstrip('/')}/api/pub?channel={quote(channel, safe='')}"
