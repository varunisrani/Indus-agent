"""The Discord adapter's contract case (P15-34): the real adapter over the Discord wire fake.

discord.py is not needed: the wire fake's ``sdk`` namespace stands in for the ``discord`` module.
"""

from __future__ import annotations

from indus.foundation.config.schema import PlatformSection
from indus.gateway.adapters.base import Adapter
from indus.gateway.adapters.discord import DiscordAdapter
from indus.gateway.config import PlatformSettings
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.discord_wire import MAX_LEN, DiscordWire


def fake_token() -> str:
    """A token-shaped value built at run time (never a literal credential)."""
    return ".".join(("MTAw" + "x" * 20, "abcdef", "z" * 27))


def discord_settings(**extra: object) -> PlatformSettings:
    return PlatformSettings(
        name="discord",
        enabled=True,
        reason="credentials",
        section=PlatformSection(extra=dict(extra)),
        credentials={"DISCORD_BOT_TOKEN": fake_token()},
    )


def _make_env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform("discord", clock=clock, max_len=MAX_LEN, bot_user_id="900")
    return CaseEnv(clock, platform, DiscordWire(platform))


def _build(env: CaseEnv) -> Adapter:
    return DiscordAdapter(clock=env.clock, settings=discord_settings(), sdk=env.wire.sdk)


async def _pump(env: CaseEnv, adapter: Adapter) -> None:
    await env.wire.dispatch_pending()


DISCORD_CASE = register_case(
    AdapterCase(
        "discord",
        _make_env,
        _build,
        user_id="200",
        group_id="300",
        thread_id="400",
        bot_id="500",
        pump=_pump,
    )
)
