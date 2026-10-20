"""The BlueBubbles adapter in the shared contract suite, over the fake server (P16-24)."""

from __future__ import annotations

from indus.gateway.adapters._http import ListenerSettings
from indus.gateway.adapters.base import Adapter
from indus.gateway.adapters.bluebubbles.adapter import (
    WEBHOOK_MAX_BODY,
    WEBHOOK_PATH,
    BlueBubblesAdapter,
)
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.bluebubbles_wire import MAX_LEN, PASSWORD, SERVER_URL, BlueBubblesWire
from tests.fakes.platform.core import FakeChatPlatform


def _env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform("bluebubbles", clock=clock, max_len=MAX_LEN)
    return CaseEnv(clock, platform, BlueBubblesWire(platform))


def build_adapter(wire: BlueBubblesWire, clock: FakeClock, **kw: object) -> BlueBubblesAdapter:
    adapter = BlueBubblesAdapter(
        clock=clock,
        server_url=SERVER_URL,
        password=PASSWORD,
        listener=ListenerSettings(port=0, max_body_bytes=WEBHOOK_MAX_BODY, path=WEBHOOK_PATH),
        transport=wire.transport(),
        serve=False,
        **kw,  # pyright: ignore[reportArgumentType]
    )
    wire.attach(adapter.listener.app)
    return adapter


def _build(env: CaseEnv) -> Adapter:
    return build_adapter(env.wire, env.clock)


async def _pump(env: CaseEnv, adapter: Adapter) -> None:
    await env.wire.pump()
    assert isinstance(adapter, BlueBubblesAdapter)
    await adapter.listener.idle()


BLUEBUBBLES_CASE = register_case(
    AdapterCase(
        "bluebubbles",
        _env,
        _build,
        user_id="+15550000200",
        group_id="iMessage;+;chat300",
        thread_id="400",
        bot_id="+15550000500",
        pump=_pump,
        # iMessage has no threads of their own (inline replies are quotes) and no bot accounts.
        lacks=frozenset({"thread", "bot"}),
    )
)
