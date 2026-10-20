"""Reverse-proxy path prefix: read ``X-Forwarded-Prefix``, rewrite served HTML and CSS (P18-05).

A proxy that mounts the dashboard under a path (``https://host/indus/``) sends the mount point in
``X-Forwarded-Prefix``. The SPA bundle is built for ``/``, so at request time:

- ``index.html``: absolute ``href``/``src`` URLs of the bundle (``/assets/``, ``/fonts/`` and the
  favicon) gain the prefix, and the base-path global (set by ``spa``) tells the SPA to prefix every
  REST/WS URL and its router basename;
- served CSS: ``url(/assets/…)``, ``url(/fonts/…)`` (bare, single- or double-quoted) gain it too.

The header is believed only from a trusted peer (``auth.proxy``); the caller passes the verdict in.
A malformed value (``..``, ``//``, quotes, angle brackets, whitespace, control characters, longer
than ``MAX_PREFIX``) is ignored with one warning per distinct value: no prefix rather than a
sanitised guess.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from typing import Final

__all__ = [
    "MAX_PREFIX",
    "PREFIX_HEADER",
    "normalise_prefix",
    "prefix_from_headers",
    "rewrite_css",
    "rewrite_index_html",
]

log = logging.getLogger(__name__)

PREFIX_HEADER: Final = "x-forwarded-prefix"
MAX_PREFIX: Final = 256
_BAD_CHARS: Final = frozenset("\"'<>`\\%?#;")
_ASSET_ROOTS: Final = ("/assets/", "/fonts/")
_FAVICONS: Final = ("/favicon.ico", "/favicon.svg", "/favicon.png")
_HTML_ATTRS: Final = ("href", "src")

_warned: set[str] = set()
_warned_lock = threading.Lock()


def _warn_once(raw: str, why: str) -> None:
    with _warned_lock:
        if raw in _warned or len(_warned) > 256:
            return
        _warned.add(raw)
    log.warning("ignored a forwarded path prefix (%s); URLs are built without one", why)


def _has_control(text: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7F or char.isspace() for char in text)


def normalise_prefix(raw: str | None) -> str:
    """``/indus`` form (leading slash, no trailing slash), or ``""`` when unset or malformed."""
    if raw is None:
        return ""
    value = raw.strip()
    if not value:
        return ""
    if _has_control(value) or any(char in _BAD_CHARS for char in value):
        _warn_once(raw, "it holds a character a path prefix cannot have")
        return ""
    if not value.startswith("/"):
        value = "/" + value
    value = value.rstrip("/")
    if not value:
        return ""
    if "//" in value or any(part in (".", "..") for part in value.split("/")):
        _warn_once(raw, "it holds an empty or dot segment")
        return ""
    if len(value) > MAX_PREFIX:
        _warn_once(raw, f"it is longer than {MAX_PREFIX} characters")
        return ""
    return value


def prefix_from_headers(headers: Mapping[str, str], *, trusted: bool) -> str:
    """The normalised prefix of a request, or ``""`` when the peer is not trusted."""
    if not trusted:
        return ""
    return normalise_prefix(headers.get(PREFIX_HEADER))


def rewrite_index_html(html: str, prefix: str) -> str:
    """``index.html`` with the bundle's absolute asset and favicon URLs under ``prefix``."""
    if not prefix:
        return html
    for attr in _HTML_ATTRS:
        for quote in ('"', "'"):
            for root in _ASSET_ROOTS:
                html = html.replace(f"{attr}={quote}{root}", f"{attr}={quote}{prefix}{root}")
            for icon in _FAVICONS:
                html = html.replace(f"{attr}={quote}{icon}", f"{attr}={quote}{prefix}{icon}")
    return html


def rewrite_css(css: str, prefix: str) -> str:
    """CSS with ``url(/assets/…)`` and ``url(/fonts/…)`` (any quoting) under ``prefix``."""
    if not prefix:
        return css
    for root in _ASSET_ROOTS:
        for quote in ("", '"', "'"):
            css = css.replace(f"url({quote}{root}", f"url({quote}{prefix}{root}")
    return css
