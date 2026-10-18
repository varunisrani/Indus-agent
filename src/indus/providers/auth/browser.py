"""Should a sign-in flow open a browser, and does its loopback callback need an SSH tunnel?

Everything here decides from an env mapping (the scope's env view), never the process env.

- A **remote session** is an SSH login (``SSH_CLIENT`` / ``SSH_TTY``) or a browser-based IDE or
  cloud shell that sets one of ``REMOTE_IDE_MARKERS``. The person's browser is then on another
  machine: a loopback callback needs ``ssh -L`` and auto-opening is pointless.
- A **graphical browser is likely** unless ``$BROWSER`` names a console browser (which would take
  over the terminal), or the host is Linux with neither ``DISPLAY`` nor ``WAYLAND_DISPLAY`` and no
  ``$BROWSER`` at all (R04.18).
"""

from __future__ import annotations

import logging
import socket
import sys
import webbrowser
from collections.abc import Mapping
from pathlib import PurePath

log = logging.getLogger(__name__)

REMOTE_SSH_MARKERS: tuple[str, ...] = ("SSH_CLIENT", "SSH_TTY")
REMOTE_IDE_MARKERS: tuple[str, ...] = (
    "CLOUD_SHELL",
    "CODESPACES",
    "CODESPACE_NAME",
    "GITPOD_WORKSPACE_ID",
    "REPL_ID",
    "STACKBLITZ",
)
CONSOLE_BROWSERS = frozenset(
    {"w3m", "lynx", "links", "links2", "elinks", "browsh", "www-browser", "carbonyl"}
)


def _set(env: Mapping[str, str], name: str) -> bool:
    return bool(env.get(name, "").strip())


def is_remote_session(env: Mapping[str, str]) -> bool:
    return any(_set(env, n) for n in (*REMOTE_SSH_MARKERS, *REMOTE_IDE_MARKERS))


def names_console_browser(value: str) -> bool:
    """Does a ``$BROWSER`` value (``cmd``, ``/path/cmd %s``, ``a:b`` lists) name a console one?"""
    for choice in value.split(":" if sys.platform != "win32" else ";"):
        words = choice.strip().split()
        if not words:
            continue
        name = PurePath(words[0]).name.lower()
        if name.endswith(".exe"):
            name = name[:-4]
        if name in CONSOLE_BROWSERS:
            return True
    return False


def graphical_browser_likely(env: Mapping[str, str], *, platform: str | None = None) -> bool:
    host = sys.platform if platform is None else platform
    chosen = env.get("BROWSER", "").strip()
    if chosen and names_console_browser(chosen):
        return False
    if host.startswith("linux") and not chosen:
        return _set(env, "DISPLAY") or _set(env, "WAYLAND_DISPLAY")
    return True


def should_open(
    env: Mapping[str, str], *, no_browser: bool = False, platform: str | None = None
) -> bool:
    """Open a browser automatically? Never on request, remote sessions or console-only hosts."""
    if no_browser or is_remote_session(env):
        return False
    return graphical_browser_likely(env, platform=platform)


def _host_name() -> str:
    try:
        return socket.gethostname() or "this-host"
    except OSError:
        return "this-host"


def tunnel_hint(port: int, env: Mapping[str, str], *, host: str | None = None) -> str:
    """The command to run on the person's own machine so the callback reaches this one."""
    user = env.get("USER", "").strip() or env.get("LOGNAME", "").strip() or "you"
    where = host if host is not None else _host_name()
    return f"ssh -L {port}:127.0.0.1:{port} {user}@{where}"


def open_url(url: str) -> bool:
    """Ask the platform to open ``url``; False when nothing opened (never raises)."""
    try:
        return bool(webbrowser.open(url))
    except Exception as exc:
        log.debug("could not open a browser: %s", exc)
        return False
