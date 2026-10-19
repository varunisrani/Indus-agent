"""The Telegram adapter in the shared contract suite (P15-33): our adapter over the wire fake."""

from __future__ import annotations

from typing import cast

from indus.gateway.adapters.base import Adapter, utf16_units
from indus.gateway.adapters.telegram import TelegramAdapter
from indus.gateway.adapters.telegram.bot_api import BotApi
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.telegram_wire import MAX_LEN, TelegramWire


def make_env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform(
        "telegram", clock=clock, max_len=MAX_LEN, len_fn=utf16_units, bot_user_id="1000"
    )
    return CaseEnv(clock, platform, TelegramWire(platform))


def build(env: CaseEnv) -> Adapter:
    wire = cast(TelegramWire, env.wire)
    return TelegramAdapter(cast(BotApi, wire.bot), clock=env.clock)


async def pump(env: CaseEnv, adapter: Adapter) -> None:
    """Let the poll loop pick the queued updates up and the dispatcher hand them over."""
    assert isinstance(adapter, TelegramAdapter)
    for _ in range(3):
        await env.clock.advance_and_settle(1.0)
        await adapter.idle()


TELEGRAM_CASE = register_case(
    AdapterCase(
        "telegram",
        make_env,
        build,
        user_id="200",
        group_id="-1001300",
        thread_id="400",
        bot_id="500",
        pump=pump,
    )
)
