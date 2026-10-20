"""The WhatsApp bridge adapter in the shared contract suite, over the fake bridge (P16-03)."""

from __future__ import annotations

from indus.gateway.adapters.base import Adapter, code_points
from indus.gateway.adapters.whatsapp.adapter import WhatsAppAdapter
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.whatsapp_wire import BOT_JID, MAX_LEN, FakeBridge, FakeLauncher


def _env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform(
        "whatsapp", clock=clock, max_len=MAX_LEN, len_fn=code_points, bot_user_id=BOT_JID
    )
    return CaseEnv(clock, platform, FakeBridge(platform))


def _build(env: CaseEnv) -> Adapter:
    bridge: FakeBridge = env.wire
    return WhatsAppAdapter(
        clock=env.clock, launcher=FakeLauncher(bridge), transport=bridge.transport
    )


WHATSAPP_CASE = register_case(
    AdapterCase(
        "whatsapp",
        _env,
        _build,
        user_id="15550000200",
        group_id="120363000000000300@g.us",
        thread_id="400",
        bot_id="15550000500",
        # WhatsApp has no threads and does not mark bot accounts.
        lacks=frozenset({"thread", "bot"}),
    )
)
