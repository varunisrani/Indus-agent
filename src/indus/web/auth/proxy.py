"""Public URL, trusted proxies and forwarded headers (P18-05).

**Public URL.** ``INDUS_DASHBOARD_PUBLIC_URL`` (a blank value counts as unset), else
``dashboard.public_url``: the complete address people reach the dashboard at. It fixes the OAuth
callback to ``<public_url>/auth/callback`` verbatim (the prefix header is not added on top), its
host joins the hosts the Host and Origin checks accept, and a non-loopback host engages the login
gate even on a loopback bind (``auth.gate``). A value without an http/https scheme or a host, or
with quotes, angle brackets, whitespace or control characters, is ignored with one warning.

**Trusted proxies.** ``dashboard.trusted_proxies``: exact addresses or bounded networks. ``*``,
``0.0.0.0/0`` and ``::/0`` (any ``/0``), names and networks with host bits set are refused when the
list is parsed, so the dashboard does not start with them. A loopback peer is always trusted.

**Forwarded headers.** ``ForwardedHeaders`` (pure ASGI, the outermost layer) believes
``X-Forwarded-Proto`` and ``X-Forwarded-For`` only when the connecting peer is trusted: the scheme
becomes the forwarded one (http/https only) and the client becomes the right-most address in the
chain that is not itself a trusted proxy. It records the verdict in the scope so the prefix header
(``indus.web.prefix``) is read only from trusted peers too. Everything downstream (rate limits, the
audit log, the ``Secure`` cookie flag) reads ``scope["client"]`` / ``scope["scheme"]`` and nothing
else; no code parses forwarding headers from an untrusted peer.
"""

from __future__ import annotations

import ipaddress
import logging
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, cast
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = [
    "PUBLIC_URL_ENV",
    "TRUSTED_SCOPE_KEY",
    "ForwardedHeaders",
    "ProxyConfigError",
    "PublicOrigin",
    "TrustedProxies",
    "callback_url",
    "client_ip",
    "normalise_public_url",
    "parse_trusted_proxies",
    "peer_is_trusted",
    "resolve_public_url",
]

log = logging.getLogger(__name__)

PUBLIC_URL_ENV: Final = "INDUS_DASHBOARD_PUBLIC_URL"
TRUSTED_SCOPE_KEY: Final = "indus.trusted_peer"
"""Scope key ``ForwardedHeaders`` sets: True when the connecting peer is a trusted proxy."""
CALLBACK_PATH: Final = "/auth/callback"
_URL_BAD_CHARS: Final = frozenset("\"'<>`\\")
_FORWARDED_SCHEMES: Final = frozenset({"http", "https"})
_MAX_FORWARDED_HOPS: Final = 32

type Network = ipaddress.IPv4Network | ipaddress.IPv6Network
type Address = ipaddress.IPv4Address | ipaddress.IPv6Address

_warned: set[tuple[str, str]] = set()
_warned_lock = threading.Lock()


class ProxyConfigError(ValueError):
    """A ``trusted_proxies`` entry the dashboard refuses to start with."""


def _warn_once(source: str, value: str, why: str) -> None:
    with _warned_lock:
        key = (source, value)
        if key in _warned or len(_warned) > 256:
            return
        _warned.add(key)
    log.warning("%s was ignored: %s", source, why)


# -- public URL -----------------------------------------------------------------------------------


def normalise_public_url(raw: object) -> str:
    """``scheme://host[:port][/path]`` without a trailing slash, or ``""`` when unusable."""
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value:
        return ""
    if any(ord(char) < 0x20 or ord(char) == 0x7F or char.isspace() for char in value):
        return ""
    if any(char in _URL_BAD_CHARS for char in value):
        return ""
    try:
        parts = urlsplit(value)
        host = parts.hostname
        parts.port  # noqa: B018 - a bad port raises here
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not host:
        return ""
    if parts.username is not None or parts.password is not None:
        return ""
    if parts.query or parts.fragment:
        return ""
    return value.rstrip("/")


@dataclass(frozen=True, slots=True)
class PublicOrigin:
    """A declared public URL and its lower-case host name."""

    url: str
    host: str

    @classmethod
    def parse(cls, raw: object) -> PublicOrigin | None:
        url = normalise_public_url(raw)
        if not url:
            return None
        host = urlsplit(url).hostname or ""
        return cls(url, host.lower())


def resolve_public_url(env: Mapping[str, str], configured: object) -> PublicOrigin | None:
    """The public URL from ``env`` (non-blank wins), else from config; malformed values warn."""
    env_raw = env.get(PUBLIC_URL_ENV, "")
    if env_raw.strip():
        found = PublicOrigin.parse(env_raw)
        if found is not None:
            return found
        _warn_once(PUBLIC_URL_ENV, env_raw, "it is not an absolute http(s) URL with a host")
    if isinstance(configured, str) and configured.strip():
        found = PublicOrigin.parse(configured)
        if found is not None:
            return found
        _warn_once("dashboard.public_url", configured, "it is not an absolute http(s) URL")
    return None


def callback_url(public: PublicOrigin | None, *, scheme: str, host_header: str, prefix: str) -> str:
    """Where the IdP sends the browser back: ``<public_url>/auth/callback`` verbatim when declared
    (the prefix header is ignored there), else built from the request (scheme, Host, prefix)."""
    if public is not None:
        return public.url + CALLBACK_PATH
    safe_scheme = scheme if scheme in _FORWARDED_SCHEMES else "http"
    return f"{safe_scheme}://{host_header}{prefix}{CALLBACK_PATH}"


# -- trusted proxies ------------------------------------------------------------------------------


def _parse_entry(entry: object) -> Network:
    if not isinstance(entry, str) or not entry.strip():
        raise ProxyConfigError("dashboard.trusted_proxies: an entry is empty or not text")
    text = entry.strip()
    if text == "*":
        raise ProxyConfigError("dashboard.trusted_proxies: '*' would trust every client")
    try:
        network = ipaddress.ip_network(text, strict=True)
    except ValueError as exc:
        raise ProxyConfigError(
            f"dashboard.trusted_proxies: {text!r} is not an IP address or network ({exc})"
        ) from None
    if network.prefixlen == 0:
        raise ProxyConfigError(
            f"dashboard.trusted_proxies: {text!r} would trust every client; list the proxy's "
            "address or a dedicated proxy network"
        )
    return network


@dataclass(frozen=True, slots=True)
class TrustedProxies:
    """Peers whose forwarding headers are believed (loopback always)."""

    networks: tuple[Network, ...] = field(default=())

    def trusts(self, host: object) -> bool:
        address = _address(host)
        if address is None:
            return False
        if address.is_loopback:
            return True
        return any(address in network for network in self.networks)


def parse_trusted_proxies(entries: object) -> TrustedProxies:
    """Parse ``dashboard.trusted_proxies``; ``ProxyConfigError`` for any refused entry."""
    if entries is None or entries == "":
        return TrustedProxies()
    if isinstance(entries, str):
        items: list[object] = [part for part in entries.split(",") if part.strip()]
    elif isinstance(entries, list | tuple):
        items = list(cast(Sequence[object], entries))
    else:
        raise ProxyConfigError("dashboard.trusted_proxies must be a list of addresses")
    return TrustedProxies(tuple(_parse_entry(item) for item in items))


def _address(host: object) -> Address | None:
    if not isinstance(host, str) or not host:
        return None
    try:
        address = ipaddress.ip_address(host.strip().strip("[]").split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def peer_is_trusted(scope: Mapping[str, Any]) -> bool:
    """What ``ForwardedHeaders`` decided for this connection (False when it did not run)."""
    return scope.get(TRUSTED_SCOPE_KEY) is True


def client_ip(scope: Mapping[str, Any]) -> str:
    """The client address after forwarding was applied, or ``""``."""
    client: object = scope.get("client")
    if isinstance(client, list | tuple) and cast(Sequence[object], client):
        first = cast(Sequence[object], client)[0]
        return first if isinstance(first, str) else ""
    return ""


def _header(scope: Mapping[str, Any], name: bytes) -> str | None:
    raw: object = scope.get("headers") or ()
    if not isinstance(raw, list | tuple):
        return None
    found: list[str] = []
    for pair in cast(Iterable[object], raw):
        if not isinstance(pair, list | tuple) or len(cast(Sequence[object], pair)) != 2:
            continue
        key, value = cast(Sequence[object], pair)
        if isinstance(key, bytes) and key.lower() == name and isinstance(value, bytes):
            found.append(value.decode("latin-1"))
    return ",".join(found) if found else None


def _forwarded_client(chain: str, proxies: TrustedProxies) -> str | None:
    hops = [part.strip() for part in chain.split(",") if part.strip()][-_MAX_FORWARDED_HOPS:]
    for hop in reversed(hops):
        address = _address(hop)
        if address is None:
            return None
        if not proxies.trusts(str(address)):
            return str(address)
    return str(_address(hops[0])) if hops else None


class ForwardedHeaders:
    """Apply ``X-Forwarded-Proto`` / ``-For`` from trusted peers only (see the module notes)."""

    def __init__(self, app: ASGIApp, *, proxies: TrustedProxies) -> None:
        self.app = app
        self.proxies = proxies

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        peer = client_ip(scope)
        trusted = self.proxies.trusts(peer)
        scope = dict(scope)
        scope[TRUSTED_SCOPE_KEY] = trusted
        if trusted:
            proto = (_header(scope, b"x-forwarded-proto") or "").split(",")[0].strip().lower()
            if proto in _FORWARDED_SCHEMES:
                if scope["type"] == "websocket":
                    scope["scheme"] = "wss" if proto == "https" else "ws"
                else:
                    scope["scheme"] = proto
            chain = _header(scope, b"x-forwarded-for")
            if chain:
                forwarded = _forwarded_client(chain, self.proxies)
                if forwarded:
                    client: object = scope.get("client")
                    port = 0
                    if isinstance(client, list | tuple) and len(cast(Sequence[object], client)) > 1:
                        raw_port = cast(Sequence[object], client)[1]
                        port = raw_port if isinstance(raw_port, int) else 0
                    scope["client"] = (forwarded, port)
        await self.app(scope, receive, send)
