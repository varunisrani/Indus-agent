"""The dashboard's per-app state (``app.state.indus``), shared by the app factory, the gates, the
WebSocket helper and the routes.

It holds what one dashboard process decided at start: the mode, the auth context (gate mode,
session token, providers, signer, audit log, Host rules), the WebSocket ticket book and internal
credential, which WebSocket features are on, the profile it serves and the machine root, the bound
address, the public origin and trusted proxies, the SPA bundle and the daemon link. Routes read it
through ``dashboard_state(app)`` and nothing else (no globals).

``resolve_profile`` is the small name → home lookup the ticket route and the WebSocket helper need
(``""`` = this backend's own profile); P18-10's per-request ``Scope`` builds on the same rules.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from indus.foundation.errors import InvalidProfileId
from indus.foundation.profiles import DEFAULT_PROFILE, is_live, normalize_id, profile_home
from indus.service.ws_auth import InternalCredential, TicketBook, stable_profile_id
from indus.web.auth.gate import AuthContext
from indus.web.auth.proxy import PublicOrigin, TrustedProxies

__all__ = ["DashboardState", "Mode", "ProfileRef", "dashboard_state"]

type Mode = Literal["dashboard", "serve"]


@dataclass(frozen=True, slots=True)
class ProfileRef:
    """A live profile: its id (name), home and the stable hash ticket books bind to."""

    name: str
    home: Path
    stable_id: str


@dataclass
class DashboardState:
    mode: Mode
    auth: AuthContext
    tickets: TicketBook
    internal: InternalCredential
    home: Path
    root: Path
    profile_name: str
    bound_host: str
    port: int
    public: PublicOrigin | None = None
    proxies: TrustedProxies = field(default_factory=TrustedProxies)
    embedded_chat: bool = True
    console_enabled: bool = False
    initial_profile: str = ""
    theme: str = "system"
    bundle: Any = None
    """The SPA bundle (``spa.SpaBundle``) in dashboard mode, None in serve mode."""
    daemon: Any = None
    """The daemon link (``app.DaemonLink``) when the process attaches to the profile daemon."""
    code_version: str = ""
    extras: dict[str, Any] = field(default_factory=dict[str, Any])
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def gated(self) -> bool:
        return self.auth.gated

    @property
    def stable_id(self) -> str:
        return stable_profile_id(self.home)

    def feature_enabled(self, purpose: str) -> bool:
        """Is the WebSocket feature behind ``purpose`` on?"""
        if purpose == "web-console":
            return self.console_enabled
        if purpose == "web-plugin":
            return True  # the plugin-route gate decides whether the plugin is on (P24-37)
        return self.embedded_chat

    def resolve_profile(self, name: str | None) -> ProfileRef | None:
        """``""``/None → this backend's profile; a live profile name → its ref; else None."""
        if not name:
            return ProfileRef(self.profile_name, self.home, self.stable_id)
        try:
            pid = normalize_id(name)
        except InvalidProfileId:
            return None
        if pid == self.profile_name:
            return ProfileRef(self.profile_name, self.home, self.stable_id)
        if not is_live(self.root, pid):
            return None
        home = profile_home(self.root, pid)
        return ProfileRef(pid, home, stable_profile_id(home))

    def own_profile_label(self) -> str:
        return self.profile_name or DEFAULT_PROFILE


def dashboard_state(app: Any) -> DashboardState:
    """The state ``create_app`` stored on ``app`` (``RuntimeError`` when it is missing)."""
    state: object = getattr(getattr(app, "state", None), "indus", None)
    if not isinstance(state, DashboardState):
        raise RuntimeError("this app was not built by indus.web.app.create_app")
    return state


StateFactory = Callable[[], DashboardState]
