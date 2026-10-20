"""The SimpleX adapter in the shared contract suite, over the fake daemon socket (P16-28).

The adapter runs through its opener seam, so the suite needs no WebSocket library.
"""

from __future__ import annotations

from indus.gateway.adapters.base import Adapter
from indus.gateway.adapters.simplex.adapter import SimplexAdapter, SimplexOptions
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.simplex_wire import MAX_LEN, URL, SimplexWire


def _env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform("simplex", clock=clock, max_len=MAX_LEN)
    return CaseEnv(clock, platform, SimplexWire(platform))


def _build(env: CaseEnv) -> Adapter:
    wire: SimplexWire = env.wire
    return SimplexAdapter(
        clock=env.clock,
        ws_url=URL,
        options=SimplexOptions(group_chats=frozenset({"*"})),
        opener=wire.open,
        rng=lambda: 0.0,
    )


async def _pump(env: CaseEnv, adapter: Adapter) -> None:
    env.wire.push_inbound()


SIMPLEX_CASE = register_case(
    AdapterCase(
        "simplex",
        _env,
        _build,
        user_id="200",
        group_id="group:300",
        thread_id="400",
        bot_id="500",
        pump=_pump,
        # SimpleX has no threads.
        lacks=frozenset({"thread"}),
    )
)
