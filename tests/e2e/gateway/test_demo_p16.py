"""The P16 demo (no new milestone): one gateway daemon running Telegram (P15), Signal and Matrix at
once, each answering, and an approval raised over Matrix answered by a typed ``/approve``.

The daemon is the real ``GatewayDaemon`` with every default part, on a fake clock, against the
scripted fake model (``tests/integration/gateway/three_platforms.Brain``). The registry keeps the
built-in rows and builds the real adapters over their wire fakes: Telegram's Bot API fake, the
fake signal-cli daemon and the fake Matrix homeserver. Each adapter receives on its own (Telegram
long-polls, Signal reads the daemon's event stream, Matrix long-polls ``/sync``).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from indus.foundation.scope import Scope, make_scope
from indus.foundation.secrets import open_secret_store
from indus.gateway.adapters import matrix as matrix_pkg
from indus.gateway.adapters import telegram as telegram_pkg
from indus.gateway.adapters.base import Adapter, utf16_units
from indus.gateway.adapters.signal.adapter import SignalAdapter
from indus.gateway.adapters.telegram.bot_api import BotApi
from indus.gateway.config import AdapterConfig
from indus.gateway.daemon.process import GatewayDaemon
from indus.gateway.registry import PlatformRegistry, builtin_entries
from indus.state.gateway_runtime import EXIT_OK
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.matrix_wire import BOT as MATRIX_BOT
from tests.fakes.platform.matrix_wire import HOMESERVER, MatrixWire
from tests.fakes.platform.signal_wire import ACCOUNT as SIGNAL_ACCOUNT
from tests.fakes.platform.signal_wire import MAX_LEN as SIGNAL_MAX
from tests.fakes.platform.signal_wire import SignalDaemon, uuid_for
from tests.fakes.platform.telegram_wire import MAX_LEN as TELEGRAM_MAX
from tests.fakes.platform.telegram_wire import TelegramWire
from tests.integration.gateway.daemon_kit import quick
from tests.integration.gateway.three_platforms import START, Brain, fake_secret, idle

pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="POSIX sockets and signals"),
    pytest.mark.slow,
]

PLATFORMS = ("telegram", "signal", "matrix")
# The one allowed person on each platform (a DM's chat id is the person's id on all three).
USERS = {"telegram": "200", "signal": "+15550000200", "matrix": "@ana:example.org"}
# Where the replies land: Signal answers the sender's account id (its UUID), not the number.
REPLY_CHATS = {**USERS, "signal": uuid_for(USERS["signal"])}
SIGNAL_URL = "http://127.0.0.1:8080"


class Wires:
    """The three wire fakes on one clock."""

    def __init__(self, clock: FakeClock) -> None:
        telegram = FakeChatPlatform(
            "telegram", clock=clock, max_len=TELEGRAM_MAX, len_fn=utf16_units, bot_user_id="1000"
        )
        signal = FakeChatPlatform(
            "signal",
            clock=clock,
            max_len=SIGNAL_MAX,
            len_fn=utf16_units,
            bot_user_id=SIGNAL_ACCOUNT,
        )
        self.telegram = TelegramWire(telegram)
        self.signal = SignalDaemon(signal)
        self.matrix = MatrixWire(clock=clock, bot_user=MATRIX_BOT)
        self.matrix.platform.add_chat(USERS["matrix"], "dm", "Ana")

    def platform(self, name: str) -> FakeChatPlatform:
        found: FakeChatPlatform = getattr(self, name).platform
        return found

    def texts(self, name: str) -> list[str]:
        return [t.replace("\\", "") for t in self.platform(name).texts(REPLY_CHATS[name])]


async def drive(
    clock: FakeClock, check: Callable[[], object], *, what: str, step: float = 0.5
) -> None:
    """Move the fake clock by ``step`` until ``check()`` holds (60 real seconds at most)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + 60.0
    while not check():
        if loop.time() > end:
            raise AssertionError(f"never happened: {what}")
        await idle()
        if not check():
            clock.advance(step)


def demo_scope(root: Path, clock: FakeClock, wires: Wires) -> Scope:
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "profile.yaml").touch()
    (home / "config.yaml").write_text(
        "model:\n  provider: openai\n  default: gpt-5\n", encoding="utf-8"
    )
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "INDUS_HOME": str(home),
        "INDUS_GATEWAY_LOCK_DIR": str(root / "token-locks"),
        "TELEGRAM_BOT_TOKEN": fake_secret("telegram"),
        "SIGNAL_ACCOUNT": SIGNAL_ACCOUNT,
        "SIGNAL_HTTP_URL": SIGNAL_URL,
        "MATRIX_ACCESS_TOKEN": wires.matrix.token,
        "MATRIX_HOMESERVER": HOMESERVER,
        "MATRIX_USER_ID": MATRIX_BOT,
        "OPENAI_API_KEY": fake_secret("openai"),
        **{f"INDUS_{name.upper()}_ALLOWED_USERS": user for name, user in USERS.items()},
    }
    scope = make_scope(home, env=env, clock=clock)
    return scope.with_secrets(open_secret_store(home, scope.profile_id, mode="file"))


def wired_registry(wires: Wires) -> PlatformRegistry:
    """The built-in rows of the three platforms, each building the real adapter over its fake."""
    rows = {entry.name: entry for entry in builtin_entries()}

    def telegram(config: AdapterConfig) -> Adapter:
        saved = telegram_pkg.BOT_FACTORY
        telegram_pkg.BOT_FACTORY = lambda _token, _config: cast(BotApi, wires.telegram.bot)
        try:
            return telegram_pkg.create_adapter(config)
        finally:
            telegram_pkg.BOT_FACTORY = saved

    def signal(config: AdapterConfig) -> Adapter:
        adapter = SignalAdapter.from_config(config)
        adapter._transport = wires.signal.transport  # pyright: ignore[reportPrivateUsage]
        return adapter

    def matrix(config: AdapterConfig) -> Adapter:
        return matrix_pkg.create_adapter(config, transport=wires.matrix.transport())

    factories: dict[str, Callable[[AdapterConfig], Adapter]] = {
        "telegram": telegram,
        "signal": signal,
        "matrix": matrix,
    }
    return PlatformRegistry(
        [
            replace(rows[name], factory=make, check_fn=lambda: True)
            for name, make in factories.items()
        ]
    )


def demo_daemon(root: Path, clock: FakeClock, wires: Wires, brain: Brain) -> GatewayDaemon:
    return GatewayDaemon(
        demo_scope(root, clock, wires),
        options=quick(),
        registry=wired_registry(wires),
        service_factory=brain.service(root / "service-locks"),
    )


async def _start(daemon: GatewayDaemon, clock: FakeClock) -> asyncio.Task[int]:
    task = asyncio.ensure_future(daemon.run())
    await drive(
        clock,
        lambda: task.done() or daemon.status.state in ("running", "degraded", "startup_failed"),
        what="the daemon running",
    )
    if task.done():
        raise AssertionError(f"the daemon ended at startup with {task.result()}")
    return task


async def _stop(daemon: GatewayDaemon, task: asyncio.Task[int], clock: FakeClock) -> int:
    if not task.done():
        daemon.request_stop("test")
    await drive(clock, task.done, what="the daemon stopped")
    return task.result()


def test_one_gateway_answers_on_telegram_signal_and_matrix(tmp_path: Path) -> None:
    clock = FakeClock(START)
    wires = Wires(clock)
    brain = Brain(tuple(text_turn("Hello from your Indus agent") for _ in PLATFORMS))
    daemon = demo_daemon(tmp_path, clock, wires, brain)

    async def main() -> None:
        task = await _start(daemon, clock)
        try:
            assert {n: daemon.platforms[n].state for n in PLATFORMS} == dict.fromkeys(
                PLATFORMS, "connected"
            )
            for name in PLATFORMS:
                wires.platform(name).dm(USERS[name], f"hello from {name}")
            await drive(
                clock,
                lambda: all("Hello from your Indus agent" in wires.texts(n) for n in PLATFORMS),
                what="an answer on every platform",
            )
        finally:
            assert await _stop(daemon, task, clock) == EXIT_OK

    asyncio.run(main())
    assert brain.calls == 3  # one agent, one model, three conversations
    for name in PLATFORMS:
        assert wires.texts(name) == ["Hello from your Indus agent"]


def test_an_approval_over_matrix_is_answered_by_a_typed_approve(tmp_path: Path) -> None:
    clock = FakeClock(START)
    wires = Wires(clock)
    brain = Brain((tool_batch(("ask", {})), text_turn("Cleaned up the build folder.")))
    daemon = demo_daemon(tmp_path, clock, wires, brain)
    room = wires.matrix.platform
    user = USERS["matrix"]

    async def main() -> None:
        task = await _start(daemon, clock)
        try:
            room.dm(user, "please clean the build folder")
            await drive(
                clock,
                lambda: any("/approve" in t for t in wires.texts("matrix")),
                what="the approval card",
            )
            (card,) = [t for t in wires.texts("matrix") if "/approve" in t]
            assert "rm -r build" in card
            assert brain.answers == []
            room.dm(user, "/approve")
            await drive(
                clock,
                lambda: "Cleaned up the build folder." in wires.texts("matrix"),
                what="the approved turn's answer",
            )
        finally:
            assert await _stop(daemon, task, clock) == EXIT_OK

    asyncio.run(main())
    assert brain.answers == ["once"]
    assert brain.calls == 2
    # the other two platforms stayed quiet
    assert wires.texts("telegram") == [] and wires.texts("signal") == []
