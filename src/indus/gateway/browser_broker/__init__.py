"""The browser-control broker (P24-47): an external browser controller paired with callers.

``broker`` holds the rules (identity-exact routing, single-use tickets, complete-once commands,
detach vs disconnect, the capability allow-list and the developer gate), ``artifacts`` the
id-only file hand-off, ``routes`` the API server's register route and controller WebSocket.
The daemon builds one ``BrowserBroker`` over its ticket book when ``browser.control_enabled`` is
on and passes it to the API server; callers reach the controller with ``BrowserBroker.call``.
"""

from __future__ import annotations

from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope

__all__ = ["control_enabled", "developer_mode"]


def _flag(scope: Scope, key: str) -> bool:
    value: object = None
    if scope.config is not None:
        try:
            value = scope.config.get(key)
        except Exception:
            value = None
    return (default_for(key) if value is None else value) is True


def control_enabled(scope: Scope) -> bool:
    """``browser.control_enabled``, read now (off by default)."""
    return _flag(scope, "browser.control_enabled")


def developer_mode(scope: Scope) -> bool:
    """``browser.control_developer_mode``, read now: raw CDP and evaluate are allowed only while
    it is on (the broker reads it again at every call)."""
    return _flag(scope, "browser.control_developer_mode")
