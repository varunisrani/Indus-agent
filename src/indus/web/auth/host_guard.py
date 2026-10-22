"""Host and Origin checks against DNS rebinding (P18-02, R09.3).

A page on ``evil.example`` whose name is re-pointed at 127.0.0.1 is same-origin with the
dashboard as far as the browser cares, but its requests still say ``Host: evil.example``. The guard
accepts only:

- the exact host the server is bound to, with or without a port;
- the loopback aliases (``localhost``, ``127.0.0.1``, ``::1``) when bound to loopback;
- the exact host names of a declared public URL (``auth.proxy``);
- any well-formed host when bound to all interfaces (``0.0.0.0`` / ``::``): no defence is possible
  at this layer there, and that bind always runs gated.

There is no wildcard and no suffix match: ``dash.example.com.evil.test`` is refused. A Host value
that is not a plain authority (a scheme, userinfo, path, query, quotes, whitespace, an unbracketed
IPv6 address, a bad port) is refused. Refusal is a 400 JSON answer.

WebSocket upgrades bypass HTTP middleware, so ``HostRules.check_upgrade`` gives the WS helper the
same decision plus the Origin rule: an http/https Origin must name an accepted host; a non-web
Origin (``file://``, ``null``, an app scheme) is accepted only on a loopback bind.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, cast
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

from indus.foundation.digits import is_digits

__all__ = [
    "ALL_INTERFACES",
    "LOOPBACK_HOSTS",
    "HostGuard",
    "HostRules",
    "host_of_authority",
    "scope_headers",
]

LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1"})
ALL_INTERFACES: Final[frozenset[str]] = frozenset({"0.0.0.0", "::"})
_BAD_AUTHORITY: Final = frozenset("\"'<>`\\/?#@ \t\r\n")
_PORT: Final = re.compile(r"^:[0-9]{1,5}$")


def host_of_authority(value: object) -> str:
    """The lower-case host of an HTTP ``Host`` authority, or ``""`` when it is not one."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or "://" in text or any(char in _BAD_AUTHORITY for char in text):
        return ""
    if any(ord(char) < 0x21 or ord(char) == 0x7F for char in text):
        return ""
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            return ""
        host = text[1:end]
        rest = text[end + 1 :]
        if ":" not in host or (rest and not _PORT.match(rest)):
            return ""
        return host.lower()
    if text.count(":") > 1:
        return ""
    host, colon, port = text.partition(":")
    if colon and not is_digits(port, max_len=5):
        return ""
    return host.lower() if host else ""


def _bare(host: str) -> str:
    return host.strip().strip("[]").lower()


@dataclass(frozen=True, slots=True)
class HostRules:
    """The accepted Host set for one listener."""

    bound_host: str
    public_hosts: frozenset[str] = field(default_factory=frozenset[str])

    @property
    def loopback_bind(self) -> bool:
        return _bare(self.bound_host) in LOOPBACK_HOSTS

    @property
    def all_interfaces(self) -> bool:
        return _bare(self.bound_host) in ALL_INTERFACES

    def accepts(self, host_header: object) -> bool:
        host = host_of_authority(host_header)
        if not host:
            return False
        if host in self.public_hosts or self.all_interfaces:
            return True
        if self.loopback_bind:
            return host in LOOPBACK_HOSTS
        return host == _bare(self.bound_host)

    def accepts_origin(self, origin: str) -> bool:
        text = origin.strip()
        try:
            parts = urlsplit(text)
        except ValueError:
            return False
        if parts.scheme not in ("http", "https"):
            return self.loopback_bind
        if not parts.netloc or parts.username is not None or parts.path not in ("", "/"):
            return False
        return self.accepts(parts.netloc)

    def check_upgrade(self, headers: Mapping[str, str]) -> str | None:
        """None when the WS upgrade's Host and Origin pass, else a short reason."""
        if not self.accepts(headers.get("host", "")):
            return "host not accepted"
        origin = headers.get("origin")
        if origin is not None and not self.accepts_origin(origin):
            return f"origin not accepted: {origin}"
        return None


def scope_headers(scope: Mapping[str, Any]) -> dict[str, str]:
    """Lower-case header map of an ASGI scope (repeated headers joined with a comma)."""
    found: dict[str, str] = {}
    raw: object = scope.get("headers") or ()
    if not isinstance(raw, list | tuple):
        return found
    for pair in cast(Iterable[object], raw):
        if not isinstance(pair, list | tuple):
            continue
        items = cast(tuple[object, ...], tuple(cast(Iterable[object], pair)))
        if len(items) != 2:
            continue
        name, value = items
        if isinstance(name, bytes) and isinstance(value, bytes):
            key = name.decode("latin-1").lower()
            text = value.decode("latin-1")
            found[key] = f"{found[key]},{text}" if key in found else text
    return found


class HostGuard:
    """HTTP middleware: 400 JSON unless ``rules`` accept the request's Host."""

    def __init__(self, app: ASGIApp, *, rules: HostRules) -> None:
        self.app = app
        self.rules = rules

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if self.rules.accepts(scope_headers(scope).get("host", "")):
            await self.app(scope, receive, send)
            return
        body = json.dumps(
            {
                "error": "bad_host",
                "detail": "The Host header must name the address the dashboard is served at.",
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
