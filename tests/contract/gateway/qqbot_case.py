"""The QQ Bot adapter in the shared contract suite, over the fake QQ API and gateway (P16-20)."""

from __future__ import annotations

from indus.gateway.adapters.base import Adapter
from indus.gateway.adapters.qqbot.adapter import QQBotAdapter
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.qqbot_wire import BOT_ID, MAX_LEN, QQWire


def _env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform("qqbot", clock=clock, max_len=MAX_LEN, bot_user_id=BOT_ID)
    return CaseEnv(clock, platform, QQWire(platform))


def _build(env: CaseEnv) -> Adapter:
    wire: QQWire = env.wire
    return QQBotAdapter(
        clock=env.clock,
        app_id=wire.app_id,
        secret=wire.secret,
        transport=wire.transport(),
        opener=wire.opener,
    )


QQBOT_CASE = register_case(
    AdapterCase(
        "qqbot",
        _env,
        _build,
        user_id="U200",
        group_id="group:G300",
        thread_id="400",
        bot_id="B500",
        # QQ has no threads and no edit events, and groups deliver only people's @-mentions.
        lacks=frozenset({"thread", "bot", "edit"}),
    )
)
