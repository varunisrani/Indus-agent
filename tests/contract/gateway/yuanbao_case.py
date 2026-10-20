"""The Yuanbao adapter in the shared contract suite, over the fake Yuanbao service (P16-21)."""

from __future__ import annotations

from indus.gateway.adapters.base import Adapter
from indus.gateway.adapters.yuanbao.adapter import YuanbaoAdapter, YuanbaoOptions
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.yuanbao_wire import BOT_ID, MAX_LEN, YuanbaoWire


def _env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform("yuanbao", clock=clock, max_len=MAX_LEN, bot_user_id=BOT_ID)
    return CaseEnv(clock, platform, YuanbaoWire(platform))


def _build(env: CaseEnv) -> Adapter:
    wire: YuanbaoWire = env.wire
    return YuanbaoAdapter(
        clock=env.clock,
        app_id=wire.app_id,
        secret=wire.secret,
        options=YuanbaoOptions(),
        transport=wire.transport(),
        opener=wire.open,
    )


YUANBAO_CASE = register_case(
    AdapterCase(
        "yuanbao",
        _env,
        _build,
        user_id="u200",
        group_id="group:300",
        thread_id="400",
        bot_id="b500",
        # Yuanbao has no threads and no edit events, and does not mark bot authors.
        lacks=frozenset({"thread", "bot", "edit"}),
    )
)
