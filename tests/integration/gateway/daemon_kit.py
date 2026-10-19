"""In-process gateway daemons for the P15-24..26 tests.

``daemon_scope`` makes a profile home under the test's tmp dir with a real clock (the daemon's
drain and watchdog timings are wall-clock), its token-lock dir inside the tmp dir and a fake
Telegram credential built at runtime. ``fake_registry`` holds platform entries whose factory
builds the P15-11 test adapter over ``FakeChatPlatform``; ``fake_service`` builds the session
service over the scripted fake model. ``quick`` gives daemon options without signal handlers,
watchdog threads or process-env changes (safe inside pytest), with short drain graces.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from indus.core.persist import StateStore
from indus.foundation.clock import SystemClock
from indus.foundation.scope import EnvView, Scope, make_scope
from indus.foundation.secrets import open_secret_store
from indus.gateway.adapters.base import Adapter, AdapterConnectError
from indus.gateway.core.events import Source
from indus.gateway.core.session_keys import build_session_key
from indus.gateway.daemon.parts import DaemonPart
from indus.gateway.daemon.process import (
    AdapterConfig,
    DaemonOptions,
    GatewayDaemon,
    ServiceFactory,
)
from indus.gateway.registry import PlatformEntry, PlatformRegistry, policy_env_name
from indus.service.session_service import SessionService
from tests.fakes.agent import eventually, fake_options
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, Turn, fake_route, script_of
from tests.fakes.platform.core import FakeChatPlatform
from tests.unit.gateway.core._kit import ALLOWED, FakeChatAdapter

__all__ = [
    "ALLOWED",
    "Flight",
    "HangingAdapter",
    "RecordingPart",
    "bot_token",
    "daemon_scope",
    "dm_key",
    "fake_entry",
    "fake_platform",
    "fake_registry",
    "fake_service",
    "fly",
    "model_server",
    "quick",
    "rebind",
]

START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


def bot_token(name: str = "telegram") -> str:
    """A fake credential, built at runtime (never a credential-shaped literal)."""
    return "fake" + "-" + name + "-" + "credential"


def daemon_scope(
    root: Path,
    *,
    profile: str = "work",
    env: Mapping[str, str | None] | None = None,
    config: str = "",
) -> Scope:
    home = root / "profiles" / profile
    home.mkdir(parents=True, exist_ok=True)
    (home / "profile.yaml").touch()
    if config:
        (home / "config.yaml").write_text(config, encoding="utf-8")
    overlay: dict[str, str | None] = {
        "INDUS_HOME": str(home),
        "INDUS_GATEWAY_LOCK_DIR": str(root / "token-locks"),
        "TELEGRAM_BOT_TOKEN": bot_token(),
        "INDUS_TELEGRAM_ALLOWED_USERS": ALLOWED,
        "NOTIFY_SOCKET": None,
        "INDUS_GATEWAY_SUPERVISED": None,
        **(env or {}),
    }
    scope = make_scope(home, env=EnvView.from_process().with_overlay(overlay), clock=SystemClock())
    return scope.with_secrets(open_secret_store(home, scope.profile_id, mode="file"))


def fake_platform(name: str = "telegram") -> FakeChatPlatform:
    return FakeChatPlatform(name, clock=FakeClock(START))


class HangingAdapter(FakeChatAdapter):
    """Never finishes connecting (or raises the given error)."""

    error: ClassVar[AdapterConnectError | None] = None

    async def _open(self, is_reconnect: bool) -> None:
        if self.error is not None:
            raise self.error
        await asyncio.get_running_loop().create_future()


def fake_entry(
    fake: FakeChatPlatform,
    *,
    name: str = "telegram",
    installed: bool = True,
    adapter: type[FakeChatAdapter] = FakeChatAdapter,
    built: list[Adapter] | None = None,
) -> PlatformEntry:
    def factory(config: AdapterConfig) -> Adapter:
        made = adapter(fake, clock=config.clock, settings=config.settings)
        if built is not None:
            built.append(made)
        return made

    return PlatformEntry(
        name=name,
        label=name.title(),
        factory=factory,
        check_fn=lambda: installed,
        required_env=(f"{name.upper()}_BOT_TOKEN",),
        install_hint=f"install the {name} extra",
        allowlist_env=policy_env_name(name, "ALLOWED_USERS"),
        allow_all_env=policy_env_name(name, "ALLOW_ALL_USERS"),
        allow_bots_env=policy_env_name(name, "ALLOW_BOTS"),
        home_env=policy_env_name(name, "HOME_CHANNEL"),
        max_len=4096,
    )


def fake_registry(*entries: PlatformEntry) -> PlatformRegistry:
    return PlatformRegistry(builtins=entries)


def model_server(*turns: Turn) -> FakeModelServer:
    """Stalls in the script wait on a fake clock nobody moves: only an interrupt ends them."""
    return FakeModelServer(script_of(*turns), clock=FakeClock(START))


def fake_service(server: FakeModelServer, lock_dir: Path) -> ServiceFactory:
    async def build(daemon: GatewayDaemon) -> SessionService:
        return SessionService(
            daemon.scope,
            fake_route(server),
            toolsets=[],
            options=fake_options(server, session_id=None),
            store=StateStore(daemon.store),
            lock_dir=lock_dir,
        )

    return build


def quick(**changes: Any) -> DaemonOptions:
    values: dict[str, Any] = {
        "install_signals": False,
        "watchdogs": False,
        "export_guard_env": False,
        "post_interrupt_grace": 3.0,
        "disconnect_timeout": 2.0,
        "takeover_wait": 2.0,
        **changes,
    }
    return DaemonOptions(**values)


def dm_key(scope: Scope, user: str = ALLOWED, platform: str = "telegram") -> str:
    source = Source(platform=platform, chat_id=user, chat_type="dm", user_id=user)
    return build_session_key(source, profile=scope.profile_id)


@dataclass
class RecordingPart(DaemonPart):
    """Records every hook call into a shared log (and can fail on demand)."""

    log: list[str]
    label: str = "part"
    fail_start: bool = False

    async def prepare(self, daemon: GatewayDaemon) -> None:
        self.log.append(f"{self.label}:prepare:{type(daemon.pipeline).__name__}")

    async def start(self, daemon: GatewayDaemon) -> None:
        self.log.append(f"{self.label}:start:{sorted(daemon.adapters)}")
        if self.fail_start:
            raise RuntimeError("cannot start")

    async def stop(self, daemon: GatewayDaemon) -> None:
        self.log.append(f"{self.label}:stop")


@dataclass
class Flight:
    """A daemon running as a task in the test's loop."""

    daemon: GatewayDaemon
    task: asyncio.Task[int]

    async def stop(self, reason: str = "test", limit: float = 30.0) -> int:
        if not self.task.done():
            self.daemon.request_stop(reason)
        return await asyncio.wait_for(self.task, limit)

    async def until(self, check: Callable[[], object], limit: float = 15.0) -> None:
        await eventually(lambda: check() or self.task.done(), limit=limit)
        if self.task.done() and not check():
            raise AssertionError(f"the daemon ended early with {self.task.result()}")


def rebind(fake: FakeChatPlatform) -> None:
    """Let a fake platform serve a new event loop (each boot runs in its own ``asyncio.run``):
    its inbound queue is bound to the loop that first waited on it."""
    waiting = fake.drain_inbound()
    fake._queue = None  # pyright: ignore[reportPrivateUsage]
    fake._pending.extend(waiting)  # pyright: ignore[reportPrivateUsage]


async def fly(daemon: GatewayDaemon, *, limit: float = 20.0) -> Flight:
    """Start ``daemon`` and wait until it runs (or ends)."""
    task = asyncio.ensure_future(daemon.run())
    flight = Flight(daemon, task)
    await eventually(
        lambda: task.done() or daemon.status.state in ("running", "degraded", "startup_failed"),
        limit=limit,
    )
    return flight


def dead_pid() -> int:
    """The pid of a process that has exited and been reaped."""
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    with contextlib.suppress(OSError):
        os.waitpid(child.pid, os.WNOHANG)
    return child.pid
