# pyright: strict
"""Language servers for a process: which one serves a file, start, reuse, reap (P24-15).

``LspManager.client_for(path)`` answers with a running ``LspClient`` or None:

1. LSP is on (``lsp.enabled``) and a server in the catalogue (config entries first) handles the
   file and is not disabled;
2. the file lies in a git work tree (no daemons for a chat in the home folder); the server's
   project root is the nearest folder with one of its markers below that work tree;
3. a server marked ``project_code`` starts only in a trusted root: inside the session's own work
   tree or a folder listed in ``lsp.trusted_workspaces`` (``trust``); otherwise it is skipped
   and listed under ``untrusted`` in the status;
4. a (server, root) pair that failed to start is in the broken set and costs nothing until
   ``restart`` (or until ``lsp.broken_retry_s`` passes, when that is above 0);
5. one spawn per pair at a time: concurrent callers wait for the same start.

The server's environment comes from ``safety.child_env`` (R02.22) plus the server's own ``env``
from config; its binary must already be on that environment's PATH (or be configured), nothing is
installed. Servers idle longer than ``lsp.idle_timeout`` (600 s; values under 30 s are raised to
30; 0 turns reaping off) are shut down by the reaper, which wakes every ``min(60, idle)``
seconds on the clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from indus.foundation.clock import Clock, SystemClock
from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope
from indus.safety.child_env import child_env
from indus.tools.lsp.client import LspClient, LspStartError
from indus.tools.lsp.servers import (
    CATALOGUE,
    ServerSpec,
    find_git_root,
    merge_config,
    project_root,
    resolve_command,
    server_for,
)

__all__ = [
    "DEFAULT_IDLE_S",
    "MIN_IDLE_S",
    "LspManager",
    "LspSettings",
    "LspStatus",
]

log = logging.getLogger(__name__)

DEFAULT_IDLE_S = 600.0
MIN_IDLE_S = 30.0
REAP_EVERY_MAX_S = 60.0
DEFAULT_WAIT_S = 5.0

type Key = tuple[str, str]
type EnvFactory = Callable[[Mapping[str, str]], dict[str, str]]
type ClientFactory = Callable[..., LspClient]


def _number(value: object, fallback: float) -> float:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int | float):
        return float(value)
    return fallback


def _paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    items = cast(list[object], value)
    return tuple(os.path.abspath(os.path.expanduser(v)) for v in items if isinstance(v, str) and v)


def idle_timeout(value: float) -> float:
    """The configured idle timeout with the floor applied (0 or less: no reaping)."""
    if value <= 0:
        return 0.0
    return max(MIN_IDLE_S, value)


@dataclass(frozen=True, slots=True)
class LspSettings:
    """The ``lsp`` config section, read once."""

    enabled: bool = True
    wait_timeout: float = DEFAULT_WAIT_S
    idle_timeout: float = DEFAULT_IDLE_S
    broken_retry_s: float = 0.0
    trusted_workspaces: tuple[str, ...] = ()
    servers: tuple[ServerSpec, ...] = CATALOGUE

    @classmethod
    def from_scope(cls, scope: Scope | None) -> LspSettings:
        config = scope.config if scope is not None else None

        def get(key: str) -> object:
            value = config.get(key) if config is not None else None
            return default_for(key) if value is None else value

        return cls(
            enabled=bool(get("lsp.enabled")),
            wait_timeout=_number(get("lsp.wait_timeout"), DEFAULT_WAIT_S),
            idle_timeout=idle_timeout(_number(get("lsp.idle_timeout"), DEFAULT_IDLE_S)),
            broken_retry_s=max(0.0, _number(get("lsp.broken_retry_s"), 0.0)),
            trusted_workspaces=_paths(get("lsp.trusted_workspaces")),
            servers=merge_config(get("lsp.servers")),
        )


@dataclass
class LspStatus:
    """What ``indus lsp status`` shows."""

    enabled: bool
    idle_timeout: float
    clients: list[dict[str, Any]] = field(default_factory=lambda: list[dict[str, Any]]())
    broken: list[dict[str, Any]] = field(default_factory=lambda: list[dict[str, Any]]())
    untrusted: list[dict[str, str]] = field(default_factory=lambda: list[dict[str, str]]())

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "idle_timeout": self.idle_timeout,
            "clients": self.clients,
            "broken": self.broken,
            "untrusted": self.untrusted,
        }


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == (
            os.path.abspath(root)
        )
    except ValueError:
        return False


class LspManager:
    """The process's language servers (see the module notes)."""

    def __init__(
        self,
        settings: LspSettings | None = None,
        *,
        scope: Scope | None = None,
        clock: Clock | None = None,
        env_factory: EnvFactory | None = None,
        client_factory: ClientFactory | None = None,
        trust: Iterable[str] = (),
    ) -> None:
        self.settings = settings or LspSettings.from_scope(scope)
        self.scope = scope
        self.clock: Clock = clock or (scope.clock if scope is not None else SystemClock())
        self._env_factory = env_factory or self._scope_env
        self._client_factory: ClientFactory = client_factory or LspClient
        self._trusted: set[str] = {os.path.abspath(p) for p in trust}
        self._clients: dict[Key, LspClient] = {}
        self._last_used: dict[Key, float] = {}
        self._spawning: dict[Key, asyncio.Future[LspClient | None]] = {}
        self._broken: dict[Key, tuple[float, str]] = {}
        self._untrusted: set[Key] = set()
        self._reaper: asyncio.Task[None] | None = None
        self._closed = False

    # -- policy -----------------------------------------------------------------------------

    def _scope_env(self, extra: Mapping[str, str]) -> dict[str, str]:
        if self.scope is None:
            raise RuntimeError("LspManager needs a scope or an env_factory")
        return child_env(self.scope, extra=extra)

    def trust(self, root: str) -> None:
        """Trust ``root`` (a session's own work tree) for servers that run project code."""
        self._trusted.add(os.path.abspath(root))

    def trusted(self, root: str) -> bool:
        roots = (*self._trusted, *self.settings.trusted_workspaces)
        return any(_inside(root, r) for r in roots)

    def server_for(self, path: str) -> ServerSpec | None:
        if not self.settings.enabled:
            return None
        spec = server_for(path, self.settings.servers)
        return None if spec is None or spec.disabled else spec

    def key_for(self, path: str) -> tuple[ServerSpec, Key] | None:
        spec = self.server_for(path)
        if spec is None:
            return None
        workspace = find_git_root(path)
        if workspace is None:
            return None
        return spec, (spec.server_id, project_root(path, workspace, spec.markers))

    def handles(self, path: str) -> bool:
        """True when an edit of ``path`` would be checked (cheap; nothing starts)."""
        found = self.key_for(path)
        if found is None:
            return False
        spec, key = found
        if spec.project_code and not self.trusted(key[1]):
            return False
        return not self._is_broken(key)

    def _is_broken(self, key: Key) -> bool:
        entry = self._broken.get(key)
        if entry is None:
            return False
        if self.clock.monotonic() >= entry[0]:
            del self._broken[key]
            return False
        return True

    def mark_broken(self, key: Key, reason: str) -> None:
        retry = self.settings.broken_retry_s
        until = self.clock.monotonic() + retry if retry > 0 else math.inf
        first = key not in self._broken
        self._broken[key] = (until, reason)
        if first:
            log.warning("language server %s for %s is unavailable: %s", key[0], key[1], reason)

    # -- clients ----------------------------------------------------------------------------

    async def client_for(self, path: str) -> LspClient | None:
        """A running client for ``path``, starting one when needed; None when LSP does not
        apply or the server cannot run (see the module notes)."""
        if self._closed:
            return None
        found = self.key_for(path)
        if found is None:
            return None
        spec, key = found
        if spec.project_code and not self.trusted(key[1]):
            self._untrusted.add(key)
            return None
        if self._is_broken(key):
            return None
        self._ensure_reaper()
        client = self._clients.get(key)
        if client is not None and client.running:
            self.touch(key)
            return client
        if client is not None:  # it died: start a fresh one
            self._clients.pop(key, None)
            self._last_used.pop(key, None)
            await client.shutdown()
        pending = self._spawning.get(key)
        if pending is not None:
            return await asyncio.shield(pending)
        future: asyncio.Future[LspClient | None] = asyncio.get_running_loop().create_future()
        self._spawning[key] = future
        try:
            client = await self._start(spec, key)
            future.set_result(client)
            return client
        except BaseException:
            if not future.done():
                future.set_result(None)
            raise
        finally:
            self._spawning.pop(key, None)

    async def _start(self, spec: ServerSpec, key: Key) -> LspClient | None:
        env = self._env_factory(spec.env)
        argv = resolve_command(spec, env.get("PATH"))
        if argv is None:
            self.mark_broken(key, f"not installed ({spec.install_hint or 'see indus lsp status'})")
            return None
        client = self._client_factory(
            spec.server_id,
            key[1],
            argv,
            env=env,
            cwd=key[1],
            init_options=spec.init_options,
            clock=self.clock,
        )
        try:
            await client.start()
        except LspStartError as exc:
            self.mark_broken(key, str(exc))
            return None
        self._clients[key] = client
        self.touch(key)
        return client

    def touch(self, key: Key) -> None:
        if key in self._clients:
            self._last_used[key] = self.clock.monotonic()

    def touch_client(self, client: LspClient) -> None:
        for key, known in self._clients.items():
            if known is client:
                self._last_used[key] = self.clock.monotonic()

    async def drop(self, path: str, reason: str) -> None:
        """Mark the pair serving ``path`` broken and stop its client (a check timed out)."""
        found = self.key_for(path)
        if found is None:
            return
        key = found[1]
        self.mark_broken(key, reason)
        client = self._clients.pop(key, None)
        self._last_used.pop(key, None)
        if client is not None:
            await client.shutdown()

    # -- reaping ----------------------------------------------------------------------------

    def _ensure_reaper(self) -> None:
        idle = self.settings.idle_timeout
        if idle <= 0 or (self._reaper is not None and not self._reaper.done()):
            return
        self._reaper = asyncio.create_task(self._reap_loop(idle), name="lsp-reaper")

    async def _reap_loop(self, idle: float) -> None:
        every = min(REAP_EVERY_MAX_S, idle)
        while not self._closed:
            await self.clock.sleep(every)
            try:
                await self.reap_idle()
            except asyncio.CancelledError:
                raise
            except Exception:  # one bad sweep must not end the reaper
                log.debug("language server reap failed", exc_info=True)

    async def reap_idle(self) -> list[Key]:
        """Shut down every client unused for ``idle_timeout`` (or dead); the keys reaped."""
        idle = self.settings.idle_timeout
        if idle <= 0:
            return []
        cutoff = self.clock.monotonic() - idle
        gone = [
            key
            for key, client in self._clients.items()
            if self._last_used.get(key, -math.inf) <= cutoff
            or not client.running
            or not os.path.isdir(key[1])  # its folder was deleted (a removed worktree)
        ]
        await self._stop_keys(gone, "idle, stopped or without its folder")
        return gone

    async def release(self, folder: str) -> int:
        """Stop the servers of ``folder`` and every root below it, and forget their failures
        (call before removing a worktree); how many were stopped."""
        top = os.path.abspath(folder)
        keys = [key for key in self._clients if _inside(key[1], top)]
        for key in [k for k in self._broken if _inside(k[1], top)]:
            del self._broken[key]
        await self._stop_keys(keys, f"released with {top}")
        return len(keys)

    async def _stop_keys(self, keys: Sequence[Key], why: str) -> None:
        clients = [self._clients.pop(key) for key in keys]
        for key in keys:
            self._last_used.pop(key, None)
        if clients:
            log.info("stopping %d language server(s): %s", len(clients), why)
            await asyncio.gather(*(c.shutdown() for c in clients), return_exceptions=True)

    # -- status and teardown ----------------------------------------------------------------

    def status(self) -> LspStatus:
        now = self.clock.monotonic()
        return LspStatus(
            enabled=self.settings.enabled,
            idle_timeout=self.settings.idle_timeout,
            clients=[
                {
                    "server": key[0],
                    "root": key[1],
                    "state": client.state,
                    "pid": client.pid,
                    "idle_s": round(now - self._last_used.get(key, now), 1),
                    "open_files": len(client.open_paths()),
                }
                for key, client in sorted(self._clients.items())
            ],
            broken=[
                {"server": key[0], "root": key[1], "reason": reason}
                for key, (until, reason) in sorted(self._broken.items())
                if now < until
            ],
            untrusted=[
                {"server": key[0], "root": key[1]}
                for key in sorted(self._untrusted)
                if not self.trusted(key[1])
            ],
        )

    @property
    def clients(self) -> Mapping[Key, LspClient]:
        return dict(self._clients)

    async def restart(self) -> int:
        """Stop every server and forget failures; the next edit starts servers again."""
        count = await self._stop_all()
        self._broken.clear()
        self._untrusted.clear()
        return count

    async def _stop_all(self) -> int:
        pending = [f for f in self._spawning.values() if not f.done()]
        if pending:
            with contextlib.suppress(Exception):
                await asyncio.wait(pending)
        clients = list(self._clients.values())
        self._clients.clear()
        self._last_used.clear()
        await asyncio.gather(*(c.shutdown() for c in clients), return_exceptions=True)
        return len(clients)

    async def close(self) -> None:
        """Stop the reaper and every server. Safe to call twice."""
        self._closed = True
        reaper, self._reaper = self._reaper, None
        if reaper is not None and not reaper.done():
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)
        await self._stop_all()


def describe_servers(
    servers: Sequence[ServerSpec], search_path: str | None
) -> list[dict[str, Any]]:
    """One row per catalogue entry for the CLI: id, label, handles, installed command, hint."""
    rows: list[dict[str, Any]] = []
    for spec in servers:
        argv = resolve_command(spec, search_path)
        rows.append(
            {
                "server": spec.server_id,
                "label": spec.label or spec.server_id,
                "handles": list(spec.handles),
                "installed": argv is not None,
                "command": argv[0] if argv else None,
                "disabled": spec.disabled,
                "trusted_only": spec.project_code,
                "install_hint": spec.install_hint,
            }
        )
    return rows
