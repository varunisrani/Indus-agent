"""Telegram, Discord and Slack together in one in-process gateway daemon (P15-36, Demo 3).

``three_scope`` makes a profile home on a fake clock with the three platforms enabled by fake
credentials (built at run time), one allowed user per platform and a Telegram home channel.
``Wires`` holds the three wire fakes, all on that clock. ``wired_registry`` keeps the built-in
registry rows but builds the **real** adapters over the fakes: Telegram through its
``create_adapter`` with the Bot API client factory pointed at the wire's bot, Discord and Slack
through their library seams. ``Brain`` is the one agent: a session service over the scripted fake
model, with an inline ``ask`` tool that asks for approval. ``three_daemon`` is a ``GatewayDaemon``
with every default part (control socket, ledger, router, extras: chat prompts, media intake,
cron). ``drive`` moves the fake clock in small steps while the Discord and Slack wires deliver
their queued events (Telegram long-polls by itself).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from indus.core.persist import StateStore
from indus.core.turn_state import TurnState
from indus.foundation.scope import Scope, make_scope
from indus.foundation.secrets import open_secret_store
from indus.gateway.adapters import discord as discord_pkg
from indus.gateway.adapters import telegram as telegram_pkg
from indus.gateway.adapters.base import Adapter, utf16_units
from indus.gateway.adapters.slack.adapter import SlackAdapter, SlackSdk
from indus.gateway.adapters.telegram.bot_api import BotApi
from indus.gateway.config import AdapterConfig
from indus.gateway.daemon.process import GatewayDaemon, ServiceFactory
from indus.gateway.extras import ExtrasPart
from indus.gateway.registry import PlatformRegistry, builtin_entries
from indus.protocol.requests_port import RequestNotAnswered
from indus.protocol.server_requests import ApprovalAnswer
from indus.service.session_service import SessionService
from indus.tools.calls import ToolCall, ToolResult
from tests.fakes.agent import fake_options
from tests.fakes.clock import FakeClock, checkpoint
from tests.fakes.model import FakeModelServer, Turn, fake_route, script_of
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.discord_wire import MAX_LEN as DISCORD_MAX
from tests.fakes.platform.discord_wire import DiscordWire
from tests.fakes.platform.slack_wire import MAX_LEN as SLACK_MAX
from tests.fakes.platform.slack_wire import SlackPlatform, SlackWire
from tests.fakes.platform.telegram_wire import MAX_LEN as TELEGRAM_MAX
from tests.fakes.platform.telegram_wire import TelegramWire
from tests.integration.gateway.daemon_kit import quick

__all__ = [
    "PLATFORMS",
    "START",
    "USERS",
    "Brain",
    "Wires",
    "drive",
    "extras_of",
    "idle",
    "start_daemon",
    "stop_daemon",
    "three_daemon",
    "three_scope",
    "wired_registry",
]

START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
PLATFORMS = ("telegram", "discord", "slack")
# The one allowed person on each platform (Slack ids are ``U…``; a DM's chat id is the user id).
USERS = {"telegram": "200", "discord": "201", "slack": "U200"}
TELEGRAM_HOME = "-100555"
CAPS = {"telegram": TELEGRAM_MAX, "discord": DISCORD_MAX, "slack": SLACK_MAX}


def fake_secret(name: str) -> str:
    """A fake credential, built at run time (never a credential-shaped literal)."""
    return "fake" + "-" + name.lower().replace("_", "-") + "-" + "value"


CONFIG = f"""\
model:
  provider: openai
  default: gpt-5
platforms:
  telegram:
    home_channel:
      chat_id: '{TELEGRAM_HOME}'
  discord:
    command_sync: 'off'
"""


def three_scope(root: Path, clock: FakeClock, *, config: str = CONFIG) -> Scope:
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "profile.yaml").touch()
    (home / "config.yaml").write_text(config, encoding="utf-8")
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "INDUS_HOME": str(home),
        "INDUS_GATEWAY_LOCK_DIR": str(root / "token-locks"),
        "TELEGRAM_BOT_TOKEN": fake_secret("telegram"),
        "DISCORD_BOT_TOKEN": fake_secret("discord"),
        "SLACK_BOT_TOKEN": fake_secret("slack-bot"),
        "SLACK_APP_TOKEN": fake_secret("slack-app"),
        "OPENAI_API_KEY": fake_secret("openai"),
        **{f"INDUS_{name.upper()}_ALLOWED_USERS": user for name, user in USERS.items()},
    }
    scope = make_scope(home, env=env, clock=clock)
    return scope.with_secrets(open_secret_store(home, scope.profile_id, mode="file"))


@dataclass
class Wires:
    telegram: TelegramWire
    discord: DiscordWire
    slack: SlackWire

    @classmethod
    def on(cls, clock: FakeClock) -> Wires:
        telegram = FakeChatPlatform(
            "telegram", clock=clock, max_len=TELEGRAM_MAX, len_fn=utf16_units, bot_user_id="1000"
        )
        discord = FakeChatPlatform("discord", clock=clock, max_len=DISCORD_MAX, bot_user_id="900")
        slack = SlackPlatform("slack", clock=clock, max_len=SLACK_MAX, bot_user_id="U0BOT")
        return cls(TelegramWire(telegram), DiscordWire(discord), SlackWire(slack))

    def platform(self, name: str) -> FakeChatPlatform:
        found: FakeChatPlatform = getattr(self, name).platform
        return found

    async def pump(self) -> None:
        """Hand the Discord and Slack fakes' queued events to their adapters' handlers."""
        await self.discord.dispatch_pending()
        await self.slack.dispatch_pending()


def wired_registry(wires: Wires) -> PlatformRegistry:
    """The built-in rows, each building the real adapter over its wire fake."""
    rows = {entry.name: entry for entry in builtin_entries()}

    def telegram(config: AdapterConfig) -> Adapter:
        saved = telegram_pkg.BOT_FACTORY
        telegram_pkg.BOT_FACTORY = lambda _token, _config: cast(BotApi, wires.telegram.bot)
        try:
            return telegram_pkg.create_adapter(config)
        finally:
            telegram_pkg.BOT_FACTORY = saved

    def discord(config: AdapterConfig) -> Adapter:
        return discord_pkg.create_adapter(config, sdk=wires.discord.sdk)

    def slack(config: AdapterConfig) -> Adapter:
        sdk = SlackSdk(
            make_app=lambda _token, _proxy: wires.slack.app,
            make_socket=lambda _app, app_token, _proxy: wires.slack.handler(app_token),
        )
        return SlackAdapter.from_config(config, sdk=sdk)

    factories: dict[str, Callable[[AdapterConfig], Adapter]] = {
        "telegram": telegram,
        "discord": discord,
        "slack": slack,
    }
    return PlatformRegistry(
        [
            replace(rows[name], factory=make, check_fn=lambda: True)
            for name, make in factories.items()
        ]
    )


@dataclass
class Brain:
    """The one agent behind every platform: the scripted fake model plus an approval tool."""

    turns: tuple[Turn, ...]
    answers: list[str] = field(default_factory=list[str])

    def __post_init__(self) -> None:
        # Stalls wait on a clock nobody moves: only an interrupt ends them.
        self.server = FakeModelServer(script_of(*self.turns), clock=FakeClock(START))

    @property
    def calls(self) -> int:
        return len(self.server.requests)

    async def ask(self, call: ToolCall, state: TurnState) -> ToolResult:
        broker = state.control.broker
        assert broker is not None
        try:
            answer = await broker.ask(
                "approval",
                {"command": "rm -r build", "description": "deletes the build folder"},
                turn_id=state.turn_id,
            )
        except RequestNotAnswered as exc:
            self.answers.append(f"not answered: {exc}")
            return ToolResult("not approved", is_error=True)
        assert isinstance(answer, ApprovalAnswer)
        self.answers.append(answer.choice.value)
        return ToolResult(f"approved: {answer.choice.value}")

    def service(self, lock_dir: Path) -> ServiceFactory:
        async def build(daemon: GatewayDaemon) -> SessionService:
            return SessionService(
                daemon.scope,
                fake_route(self.server),
                toolsets=[],
                options=fake_options(self.server, session_id=None, inline={"ask": self.ask}),
                store=StateStore(daemon.store),
                lock_dir=lock_dir,
            )

        return build


def three_daemon(
    scope: Scope, wires: Wires, brain: Brain, root: Path, **options: Any
) -> GatewayDaemon:
    return GatewayDaemon(
        scope,
        options=quick(**options),
        registry=wired_registry(wires),
        service_factory=brain.service(root / "service-locks"),
    )


def extras_of(daemon: GatewayDaemon) -> ExtrasPart:
    (part,) = [p for p in daemon.parts if isinstance(p, ExtrasPart)]
    return part


async def idle(rounds: int = 3) -> None:
    """Let the loop and worker threads run a little (real time, short slices)."""
    for _ in range(rounds):
        await checkpoint()
        waiter = asyncio.get_running_loop().create_future()
        await asyncio.wait({waiter}, timeout=0.003)
        waiter.cancel()


async def drive(
    clock: FakeClock,
    wires: Wires,
    check: Callable[[], object],
    *,
    what: str,
    step: float = 0.5,
    limit: float = 60.0,
) -> None:
    """Move the fake clock by ``step`` until ``check()`` holds (``limit`` real seconds)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + limit
    while not check():
        if loop.time() > end:
            raise AssertionError(f"never happened: {what}")
        await wires.pump()
        await idle()
        if not check():
            clock.advance(step)


async def start_daemon(daemon: GatewayDaemon, clock: FakeClock, wires: Wires) -> asyncio.Task[int]:
    task = asyncio.ensure_future(daemon.run())
    await drive(
        clock,
        wires,
        lambda: task.done() or daemon.status.state in ("running", "degraded", "startup_failed"),
        what="the daemon running",
    )
    if task.done():
        raise AssertionError(f"the daemon ended at startup with {task.result()}")
    return task


async def stop_daemon(
    daemon: GatewayDaemon, task: asyncio.Task[int], clock: FakeClock, wires: Wires
) -> int:
    if not task.done():
        daemon.request_stop("test")
    await drive(clock, wires, task.done, what="the daemon stopped")
    return task.result()
