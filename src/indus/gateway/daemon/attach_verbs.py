"""The control socket's attach verbs (P17-11): ``session-ticket`` and ``describe``.

A client attaching to the daemon (the classic CLI, the TUI's grant helper, the Python channel)
first asks ``describe`` for the daemon's attach descriptor, then ``session-ticket`` for a
single-use WebSocket ticket (``service.ws_auth``), and dials ``ws_url`` with it within 30 s. The
client half is ``service.gateway_control.session_ticket`` / ``describe``.

``session-ticket {purpose, profile_id?, instance_id?}`` → ``{ticket, ws_url, profile_id,
instance_id, expires_in}``. The control server answers it only to a peer the OS confirmed as this
user (``control_socket.OWNER_ONLY_VERBS``). Refusals (``VerbRefused`` codes): ``bad_params``
(unknown key or wrong type), ``bad_purpose``, ``profile_mismatch``, ``stale_instance``,
``unsafe_home`` (the home became writable by others), ``ws_unavailable`` (no WebSocket endpoint
yet), ``capacity``.

``describe`` → ``{profile_id, instance_id, code_version, protocol_version, ws_url,
creatable_sources, capabilities}``. Both verbs run on the daemon's loop and only read values the
daemon already holds, so they answer while turns run.

The ticket is never logged; the book keeps only its digest.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from indus.gateway.control_socket import ControlServer, Handler, VerbRefused, code_version
from indus.protocol.methods import PROTOCOL_VERSION
from indus.service.gateway_control import home_is_safe
from indus.service.ws_auth import TicketBook, TicketRefused

__all__ = [
    "ATTACH_VERBS",
    "VERB_SECONDS",
    "AttachHost",
    "AttachVerbs",
]

log = logging.getLogger(__name__)

ATTACH_VERBS = ("session-ticket", "describe")
VERB_SECONDS = 2.0
_TICKET_PARAMS = frozenset({"purpose", "profile_id", "instance_id"})


class AttachHost(Protocol):
    """What ``describe`` reads from the daemon's session host (P17-08). Both calls are cheap and
    never wait on a turn."""

    def creatable_sources(self) -> Sequence[str]:
        """Surface kinds a client may create sessions as (``cli``, ``tui``, ...)."""
        ...

    def capabilities(self) -> Sequence[str]:
        """Capability names the host offers attached clients."""
        ...


def _text_param(params: Mapping[str, Any], key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 128:
        raise VerbRefused("bad_params", f"{key} must be a short string")
    return value


class AttachVerbs:
    """The two verbs for the daemon serving ``home`` (see the module notes). ``ws_url`` returns
    the WebSocket endpoint's URL, or None while it is not up."""

    def __init__(
        self,
        home: Path,
        book: TicketBook,
        *,
        host: AttachHost | None = None,
        ws_url: Callable[[], str | None] = lambda: None,
        version: str | None = None,
    ) -> None:
        self.home = home
        self.book = book
        self.host = host
        self._ws_url = ws_url
        self._version = version if version is not None else code_version()

    # -- verbs ----------------------------------------------------------------------------------

    async def session_ticket(self, params: dict[str, Any]) -> dict[str, Any]:
        extra = set(params) - _TICKET_PARAMS
        if extra:
            raise VerbRefused("bad_params", f"unknown params: {', '.join(sorted(extra))}")
        purpose = _text_param(params, "purpose")
        if purpose is None:
            raise VerbRefused("bad_params", "purpose is required")
        if purpose not in self.book.purposes:
            raise VerbRefused("bad_purpose", f"no ticket purpose {purpose!r}")
        wanted_profile = _text_param(params, "profile_id")
        if wanted_profile is not None and wanted_profile != self.book.profile_id:
            raise VerbRefused("profile_mismatch", "this daemon serves another profile")
        wanted_instance = _text_param(params, "instance_id")
        if wanted_instance is not None and wanted_instance != self.book.instance_id:
            raise VerbRefused("stale_instance", "the daemon restarted since that describe")
        if not home_is_safe(self.home):
            raise VerbRefused("unsafe_home", "the profile home is writable by others")
        url = self._ws_url()
        if not url:
            raise VerbRefused("ws_unavailable", "the WebSocket endpoint is not up")
        try:
            minted = self.book.mint(purpose)
        except TicketRefused as refused:
            raise VerbRefused(refused.reason, str(refused)) from None
        log.info("minted a %s ticket (expires in %.0f s)", purpose, minted.expires_in)
        return {
            "ticket": minted.ticket,
            "ws_url": url,
            "profile_id": minted.profile_id,
            "instance_id": minted.instance_id,
            "expires_in": minted.expires_in,
        }

    async def describe(self, params: dict[str, Any]) -> dict[str, Any]:
        if params:
            raise VerbRefused("bad_params", f"unknown params: {', '.join(sorted(params))}")
        host = self.host
        return {
            "profile_id": self.book.profile_id,
            "instance_id": self.book.instance_id,
            "code_version": self._version,
            "protocol_version": PROTOCOL_VERSION,
            "ws_url": self._ws_url() or None,
            "creatable_sources": list(host.creatable_sources()) if host is not None else [],
            "capabilities": list(host.capabilities()) if host is not None else [],
        }

    # -- wiring ---------------------------------------------------------------------------------

    def handlers(self) -> dict[str, tuple[Handler, float]]:
        """``{verb: (handler, seconds)}``, the shape ``DaemonPorts.control_handlers`` returns."""
        return {
            "session-ticket": (self.session_ticket, VERB_SECONDS),
            "describe": (self.describe, VERB_SECONDS),
        }

    def install(self, control: ControlServer) -> None:
        """Register both verbs on ``control`` (``session-ticket`` owner-only)."""
        for verb, (handler, seconds) in self.handlers().items():
            control.register(verb, handler, timeout=seconds, owner_only=verb == "session-ticket")

    def close(self) -> None:
        """Daemon stop: every outstanding ticket stops working."""
        self.book.revoke_all()
