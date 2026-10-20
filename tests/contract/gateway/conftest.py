"""The shared adapter contract: case registry, socket guard and fake clock (P15-01).

Every platform adapter proves itself against the same suite (``test_adapter_contract.py``).
P15-33..35 and P16 register their adapters here with `register_case`; each case says how to
build its fake service (a wire fake over a `FakeChatPlatform`) and the adapter on top of it.

Checks run on a fresh `FakeClock` per check (``run_check``); the root ``fake_clock`` fixture
serves single tests. The socket guard is autouse for this directory: any real ``connect()``
raises and is recorded, so an adapter that swallows the error still fails the no-network check.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from indus.gateway.adapters.base import Adapter
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform

CLOCK_START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


@dataclass
class CaseEnv:
    """One fake service for one check: the clock, the platform and (optionally) its wire fake."""

    clock: FakeClock
    platform: FakeChatPlatform
    wire: Any = None


@dataclass(frozen=True)
class AdapterCase:
    name: str
    make_env: Callable[[FakeClock], CaseEnv]
    build: Callable[[CaseEnv], Adapter]
    # Ids valid on this platform (Telegram and Discord need numbers).
    user_id: str = "200"
    group_id: str = "300"
    thread_id: str = "400"
    bot_id: str = "500"
    # Deliver queued inbound traffic to the adapter; the default advances the clock a little.
    pump: Callable[[CaseEnv, Adapter], Awaitable[None]] | None = None
    # Inbound shapes the platform has no way to express (``group``, ``thread``, ``bot``,
    # ``edit``, ``media``, ``caption``): its wire fake has no form for them (or one the adapter
    # must drop), and the inbound check then expects no event for them instead of a normalised
    # one (P16).
    lacks: frozenset[str] = frozenset()


# P15-33..35 and P16 append their cases (``register_case``); the echo case lives in the suite.
ADAPTER_CASES: list[AdapterCase] = []


def register_case(case: AdapterCase) -> AdapterCase:
    ADAPTER_CASES.append(case)
    return case


async def settle[T](env: CaseEnv, awaitable: Awaitable[T], *, step: float = 0.5) -> T:
    """Await ``awaitable`` while moving the fake clock past any sleeps it takes."""
    task = asyncio.ensure_future(awaitable)
    for _ in range(2000):
        await env.clock.run_until_idle()
        if task.done():
            return task.result()
        env.clock.advance(step)
    task.cancel()
    raise AssertionError("contract[harness]: the operation never finished on the fake clock")


async def pump(case: AdapterCase, env: CaseEnv, adapter: Adapter) -> None:
    if case.pump is not None:
        await case.pump(env, adapter)
    for _ in range(4):
        await env.clock.advance_and_settle(1.0)


def run_check(case: AdapterCase, check: Callable[..., Awaitable[None]]) -> None:
    """Build a fresh service and adapter for ``case`` and run one contract check on them."""

    async def main() -> None:
        env = case.make_env(FakeClock(CLOCK_START))
        adapter = case.build(env)
        try:
            await check(case, env, adapter)
        finally:
            await adapter.disconnect()

    asyncio.run(main())


# -- the socket guard ---------------------------------------------------------------------------


class RealNetworkUsed(RuntimeError):
    pass


@dataclass
class SocketGuard:
    attempts: list[object] = field(default_factory=list[object])

    def refuse(self, address: object) -> RealNetworkUsed:
        self.attempts.append(address)
        return RealNetworkUsed(f"real network connect to {address!r} in an adapter test")


@pytest.fixture(autouse=True)
def socket_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[SocketGuard]:
    guard = SocketGuard()

    def connect(self: socket.socket, address: object) -> None:
        raise guard.refuse(address)

    def connect_ex(self: socket.socket, address: object) -> int:
        raise guard.refuse(address)

    def create_connection(address: object, *args: object, **kwargs: object) -> socket.socket:
        raise guard.refuse(address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    yield guard


# -- platform cases ------------------------------------------------------------------------------
# Each module registers its case on import (``register_case`` is defined above).
from tests.contract.gateway import discord_case as _discord_case  # noqa: E402, F401
from tests.contract.gateway import email_case as _email_case  # noqa: E402, F401
from tests.contract.gateway import irc_case as _irc_case  # noqa: E402, F401
from tests.contract.gateway import line_case as _line_case  # noqa: E402, F401
from tests.contract.gateway import matrix_case as _matrix_case  # noqa: E402, F401
from tests.contract.gateway import mattermost_case as _mattermost_case  # noqa: E402, F401
from tests.contract.gateway import ntfy_case as _ntfy_case  # noqa: E402, F401
from tests.contract.gateway import signal_case as _signal_case  # noqa: E402, F401
from tests.contract.gateway import slack_case as _slack_case  # noqa: E402, F401
from tests.contract.gateway import sms_case as _sms_case  # noqa: E402, F401
from tests.contract.gateway import telegram_case as _telegram_case  # noqa: E402, F401
from tests.contract.gateway import whatsapp_cloud_case as _whatsapp_cloud_case  # noqa: E402, F401
