"""Real-profile mode (P24-38): the agent drives the user's own browser profile, opt-in only.

With ``browser.real_profile: true`` a task's browser is Chromium launched directly on the user's
profile folder (``browser.real_profile_dir``, else the platform's default Chromium folder), so the
user's sign-ins are there. Nothing is copied out of the folder, and closing the session leaves the
folder exactly where it was (only the browser closes). The switch is read on every session start,
so turning it off takes effect for the next session.

The profile can be open in one browser at a time. Inside this process one task holds it under the
dedicated session name ``real-profile``; another task that wants it waits up to
``LOCK_WAIT_S`` (30 s, on the manager's clock) for the holder to close, then gets a clear refusal.
When another browser already has the folder open (Chromium's ``SingletonLock`` is there) the
session is refused and the user is told to close that browser; the lock file is never touched.

Chromium 136 and later refuse remote control of their *default* data folder; users of Google
Chrome stable point ``browser.real_profile_dir`` at a separate profile folder they signed into.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from indus.foundation.clock import Clock
from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope
from indus.platform import facts

if TYPE_CHECKING:
    from indus.tools.builtin.browser.engines import Opened, SessionHost

__all__ = [
    "LOCK_WAIT_S",
    "REAL_PROFILE_SESSION",
    "RealProfileRefused",
    "RealProfiles",
    "default_profile_dirs",
    "enabled",
    "in_use",
    "profile_dir_for",
]

log = logging.getLogger(__name__)

REAL_PROFILE_SESSION = "real-profile"
LOCK_WAIT_S = 30.0
LOCK_FILES = ("SingletonLock", "lockfile")
_PREFIX = "browser.real_profile is on, but "


class RealProfileRefused(RuntimeError):
    """The user's profile cannot be used right now (the text says why and what to do)."""


def _setting(scope: Scope, key: str) -> object:
    value: object = None
    if scope.config is not None:
        try:
            value = scope.config.get(key)
        except Exception:
            log.debug("config %s unreadable; using the default", key, exc_info=True)
    return default_for(key) if value is None else value


def enabled(scope: Scope) -> bool:
    """The user's consent, read now (never cached)."""
    return _setting(scope, "browser.real_profile") is True


def default_profile_dirs(env: Mapping[str, str], family: str | None = None) -> list[Path]:
    """Where Chromium-family browsers keep their data folder on this platform, most common first."""
    family = family or facts.os_family()
    home = env.get("HOME") or env.get("USERPROFILE") or ""
    if family == "win32":
        local = env.get("LOCALAPPDATA") or (str(Path(home) / "AppData" / "Local") if home else "")
        if not local:
            return []
        base = Path(local)
        return [
            base / "Google" / "Chrome" / "User Data",
            base / "Microsoft" / "Edge" / "User Data",
            base / "BraveSoftware" / "Brave-Browser" / "User Data",
            base / "Chromium" / "User Data",
        ]
    if not home:
        return []
    if family == "darwin":
        base = Path(home) / "Library" / "Application Support"
        return [
            base / "Google" / "Chrome",
            base / "Microsoft Edge",
            base / "BraveSoftware" / "Brave-Browser",
            base / "Chromium",
        ]
    config = Path(env.get("XDG_CONFIG_HOME") or Path(home) / ".config")
    return [
        config / "google-chrome",
        config / "microsoft-edge",
        config / "BraveSoftware" / "Brave-Browser",
        config / "chromium",
    ]


def profile_dir_for(scope: Scope) -> Path | None:
    """The folder real-profile mode would open: the setting, else the first default that exists."""
    chosen = _setting(scope, "browser.real_profile_dir")
    if isinstance(chosen, str) and chosen.strip():
        return Path(chosen.strip()).expanduser()
    for candidate in default_profile_dirs(scope.env):
        if candidate.is_dir():
            return candidate
    return None


def in_use(folder: Path) -> bool:
    """True when a browser holds ``folder`` open (its lock entry exists, even dangling)."""
    return any(os.path.lexists(folder / name) for name in LOCK_FILES)


class RealProfiles:
    """The one real-profile browser of this process: who holds it, and the wait for it."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._lock = asyncio.Lock()
        self.holder: str | None = None

    @property
    def held(self) -> bool:
        return self._lock.locked()

    async def acquire(self, task_id: str) -> None:
        """Hold the profile for ``task_id``, waiting up to ``LOCK_WAIT_S`` on the clock."""
        if not self._lock.locked():
            await self._lock.acquire()
            self.holder = task_id
            return
        waiting = asyncio.ensure_future(self._lock.acquire())
        timer = asyncio.ensure_future(self.clock.sleep(LOCK_WAIT_S))
        try:
            await asyncio.wait({waiting, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
        if waiting.done() and not waiting.cancelled() and waiting.exception() is None:
            self.holder = task_id
            return
        if not waiting.cancel() and not waiting.cancelled() and waiting.exception() is None:
            self.holder = task_id  # it was granted while we were giving up: keep it
            return
        holder = self.holder or "another task"
        raise RealProfileRefused(
            f"{_PREFIX}the {REAL_PROFILE_SESSION} browser is held by {holder} and was not "
            f"released within {LOCK_WAIT_S:g} s. Try again when that task is done."
        )

    def release(self) -> None:
        if self._lock.locked():
            self.holder = None
            self._lock.release()

    async def open(self, host: SessionHost, scope: Scope, task_id: str) -> Opened:
        """Launch Chromium on the user's own folder for ``task_id`` (see the module notes)."""
        from indus.tools.builtin.browser.engines import Opened

        folder = profile_dir_for(scope)
        if folder is None:
            raise RealProfileRefused(
                f"{_PREFIX}no Chromium profile folder was found. Set browser.real_profile_dir "
                "to your browser's profile folder, or turn real-profile mode off."
            )
        if not folder.is_dir():
            raise RealProfileRefused(
                f"{_PREFIX}the profile folder {folder} does not exist. Fix "
                "browser.real_profile_dir, or turn real-profile mode off."
            )
        await self.acquire(task_id)
        try:
            if in_use(folder):
                raise RealProfileRefused(
                    f"{_PREFIX}another browser has the profile at {folder} open. Close that "
                    "browser first (the agent never copies your profile), then try again."
                )
            spec = host.launch_spec(scope, folder)
            try:
                handle = await host.driver().launch(spec)
            except Exception as exc:
                raise RealProfileRefused(
                    f"{_PREFIX}Chromium could not open the profile ({type(exc).__name__}). "
                    "Chrome 136 and later refuse remote control of their default folder: point "
                    "browser.real_profile_dir at a separate profile folder you signed into."
                ) from exc
            handle = await host.guarded(handle, scope)
        except BaseException:
            self.release()
            raise

        async def let_go() -> None:
            with contextlib.suppress(Exception):
                self.release()

        log.info("real-profile browser opened for %s", task_id)
        return Opened(handle, "real-profile", cleanups=[let_go], user_dir=folder)
