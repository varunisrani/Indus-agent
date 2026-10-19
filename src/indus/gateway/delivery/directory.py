"""The channel directory: which chats each connected platform can reach (P15-20).

Built when the gateway starts and every ``REFRESH_SECONDS`` after, from each connected adapter's
``list_channels()``; a platform that cannot list its chats gets the chats the routes index has
seen there (where people already talk to the bot). Saved as JSON in the profile home
(``gateway/channel_directory.json``, 0600, atomic) so another process — ``indus send`` — can turn
a name such as ``#general`` into a chat id and list what can be reached.

Name lookup (``resolve``), first rule that matches wins: an exact chat id; the name without a
leading ``#`` and case (``#General`` = ``general``), also as ``guild/name``; a prefix that only one
chat's name starts with. Anything else is not found (the router reports ``unknown_channel``).

A corrupt or unwritable file never breaks delivery: the directory is then empty or memory-only.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any, Final, cast

from indus.foundation.blocking import run_blocking
from indus.foundation.fsio import atomic_write_json
from indus.foundation.home import secure_dir
from indus.foundation.scope import Scope
from indus.gateway.adapters.base import Adapter, ChannelEntry
from indus.state import gateway_routes as routes
from indus.state.registry import StoreHandle

__all__ = ["REFRESH_SECONDS", "ChannelDirectory", "entries_from_routes", "stop"]

log = logging.getLogger(__name__)

REFRESH_SECONDS: Final = 300.0
# Platforms whose chats are not send targets (HTTP surfaces).
_NOT_TARGETS: Final = frozenset({"api_server", "webhook", "local"})


def _norm(name: str) -> str:
    return name.strip().lstrip("#").strip().lower()


def entries_from_routes(store: StoreHandle, platform: str) -> list[ChannelEntry]:
    """Chats the routes index has seen on ``platform``, one entry per chat (and thread)."""
    found: list[ChannelEntry] = []
    seen: set[tuple[str, str | None]] = set()
    for route in routes.list_routes(store, platform=platform):
        origin = route.origin
        chat_id = str(origin.get("chat_id") or "")
        if not chat_id:
            continue
        thread = origin.get("thread_id")
        thread_id = str(thread) if thread else None
        if (chat_id, thread_id) in seen:
            continue
        seen.add((chat_id, thread_id))
        name = str(
            origin.get("chat_name") or origin.get("user_name") or route.display_name or chat_id
        )
        if thread_id:
            name = f"{name} / {thread_id}"
        kind = str(origin.get("chat_type") or route.chat_type or "dm")
        found.append(ChannelEntry(chat_id, name, kind, thread_id=thread_id))
    return found


def _entry(raw: object) -> ChannelEntry | None:
    if not isinstance(raw, Mapping):
        return None
    item = cast(Mapping[str, Any], raw)
    chat_id = str(item.get("id") or "").strip()
    if not chat_id:
        return None
    thread = item.get("thread_id")
    return ChannelEntry(
        id=chat_id,
        name=str(item.get("name") or chat_id),
        type=str(item.get("type") or "channel"),
        guild=str(item.get("guild") or ""),
        thread_id=str(thread) if thread else None,
    )


class ChannelDirectory:
    """The directory for one profile (see the module notes)."""

    def __init__(self, scope: Scope, path: Path | None = None) -> None:
        self.scope = scope
        self.path = path if path is not None else scope.home / "gateway" / "channel_directory.json"
        self._lock = threading.Lock()
        self._platforms: dict[str, list[ChannelEntry]] = {}
        self.updated_at: str | None = None

    # -- reading ---------------------------------------------------------------------------------

    def load(self) -> ChannelDirectory:
        """Read the saved directory (missing or corrupt → empty); returns self."""
        try:
            raw: object = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as exc:
            log.warning("channel directory unreadable, starting empty: %s", type(exc).__name__)
            raw = {}
        platforms: dict[str, list[ChannelEntry]] = {}
        updated: str | None = None
        if isinstance(raw, dict):
            data = cast(dict[str, Any], raw)
            stamp = data.get("updated_at")
            updated = stamp if isinstance(stamp, str) else None
            listed = data.get("platforms")
            if isinstance(listed, dict):
                for name, items in cast(dict[str, Any], listed).items():
                    if isinstance(items, list):
                        entries = [_entry(i) for i in cast(list[object], items)]
                        platforms[str(name)] = [e for e in entries if e is not None]
        with self._lock:
            self._platforms = platforms
            self.updated_at = updated
        return self

    def platforms(self) -> list[str]:
        with self._lock:
            return sorted(self._platforms)

    def entries(self, platform: str) -> list[ChannelEntry]:
        with self._lock:
            return list(self._platforms.get(platform.strip().lower(), []))

    def resolve(self, platform: str, query: str) -> tuple[str, str | None] | None:
        """``(chat_id, thread_id)`` for a chat id or name on ``platform`` (see the module notes)."""
        entries = self.entries(platform)
        raw = query.strip()
        if not entries or not raw:
            return None
        for entry in entries:
            if entry.id == raw:
                return entry.id, entry.thread_id
        wanted = _norm(raw)
        for entry in entries:
            if _norm(entry.name) == wanted:
                return entry.id, entry.thread_id
        if "/" in wanted:
            guild, _, name = wanted.rpartition("/")
            for entry in entries:
                if entry.guild.strip().lower() == guild and _norm(entry.name) == name:
                    return entry.id, entry.thread_id
        starts = [e for e in entries if wanted and _norm(e.name).startswith(wanted)]
        if len(starts) == 1:
            return starts[0].id, starts[0].thread_id
        return None

    def render(self, platforms: Sequence[str] | None = None) -> str:
        """A readable list of targets (``indus send --list``)."""
        names = sorted(platforms) if platforms is not None else self.platforms()
        if not names:
            return "No messaging platforms connected yet, so there is nothing to list."
        lines: list[str] = []
        for name in names:
            lines.append(f"{name}:")
            entries = self.entries(name)
            if not entries:
                lines.append(f"  (no chats known yet; use {name}:<chat id>, or {name} alone for")
                lines.append("   the home channel)")
            for entry in entries:
                label = f"#{entry.name}" if entry.guild else entry.name
                where = entry.id if entry.thread_id is None else f"{entry.id}:{entry.thread_id}"
                lines.append(f"  {name}:{label}  [{where}] ({entry.type})")
            lines.append("")
        return "\n".join(lines).rstrip()

    def as_json(self) -> dict[str, Any]:
        with self._lock:
            return {
                "updated_at": self.updated_at,
                "platforms": {
                    name: [
                        {k: v for k, v in asdict(e).items() if v not in ("", None)} for e in items
                    ]
                    for name, items in sorted(self._platforms.items())
                },
            }

    # -- building --------------------------------------------------------------------------------

    def _save(self) -> None:
        try:
            secure_dir(self.path.parent)
            atomic_write_json(self.path, self.as_json(), sort_keys=True, mode=0o600)
        except OSError as exc:
            log.warning("channel directory not saved (kept in memory): %s", type(exc).__name__)

    async def build(
        self, adapters: Mapping[str, Adapter], store: StoreHandle | None = None
    ) -> dict[str, list[ChannelEntry]]:
        """Rebuild from the connected ``adapters`` (and the routes index) and save it."""
        platforms: dict[str, list[ChannelEntry]] = {}
        for raw_name, adapter in adapters.items():
            name = raw_name.strip().lower()
            if name in _NOT_TARGETS:
                continue
            listed: Sequence[ChannelEntry] | None = None
            try:
                listed = await adapter.list_channels()
            except Exception as exc:
                log.warning("channel directory: %s could not list its chats: %s", name, exc)
            if listed is not None:
                unique: dict[tuple[str, str | None], ChannelEntry] = {}
                for entry in listed:
                    if entry.id:
                        unique.setdefault((entry.id, entry.thread_id), entry)
                platforms[name] = list(unique.values())
                continue
            if store is None:
                platforms[name] = []
                continue
            try:
                platforms[name] = await run_blocking(
                    functools.partial(entries_from_routes, store, name), label="directory"
                )
            except Exception as exc:
                log.warning("channel directory: routes for %s unreadable: %s", name, exc)
                platforms[name] = []
        stamp = self.scope.clock.now().isoformat(timespec="seconds")
        with self._lock:
            self._platforms = platforms
            self.updated_at = stamp
        await run_blocking(self._save, label="directory save")
        return platforms

    async def run(
        self,
        adapters: Callable[[], Mapping[str, Adapter]],
        store: StoreHandle | None = None,
        *,
        interval: float = REFRESH_SECONDS,
    ) -> None:
        """Build now, then every ``interval`` seconds until cancelled; a failure never ends it."""
        while True:
            try:
                await self.build(adapters(), store)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("channel directory refresh failed", exc_info=True)
            await self.scope.clock.sleep(interval)

    def start(
        self,
        adapters: Callable[[], Mapping[str, Adapter]],
        store: StoreHandle | None = None,
        *,
        interval: float = REFRESH_SECONDS,
    ) -> asyncio.Task[None]:
        """``run`` as a task on the running loop (the daemon cancels it at shutdown)."""
        return asyncio.get_running_loop().create_task(
            self.run(adapters, store, interval=interval), name="channel directory"
        )


async def stop(task: asyncio.Task[None] | None) -> None:
    """Cancel a directory task started with ``ChannelDirectory.start``."""
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
