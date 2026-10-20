"""The Buzz adapter in the shared contract suite, over the fake relay (P16-29).

The adapter runs through its socket opener and with the fake signer, so the suite needs neither
httpx-ws nor coincurve. Ids are Nostr-shaped: users are 64-hex pubkeys (a DM goes to the user's DM
conversation), the channel is a UUID, a thread root is an event id.
"""

from __future__ import annotations

from indus.gateway.adapters.base import Adapter
from indus.gateway.adapters.buzz.adapter import BuzzAdapter, BuzzOptions
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.buzz_wire import BOT_KEY, MAX_LEN, RELAY_URL, BuzzRelay
from tests.fakes.platform.core import FakeChatPlatform

USER = "20" * 32
GROUP = "c0ffee00-0000-4000-8000-000000000300"
THREAD = "40" * 32
BOT = "50" * 32


def _env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform("buzz", clock=clock, max_len=MAX_LEN)
    platform.add_chat(GROUP, "group", "general")
    return CaseEnv(clock, platform, BuzzRelay(platform))


def build_adapter(relay: BuzzRelay, clock: FakeClock, **kw: object) -> BuzzAdapter:
    options = kw.pop("options", None) or BuzzOptions(channels=(GROUP,), poll_interval=1.0)
    return BuzzAdapter(
        clock=clock,
        relay_url=RELAY_URL,
        private_key=BOT_KEY,
        options=options,  # pyright: ignore[reportArgumentType]
        crypto=relay.crypto,
        opener=relay.open,
        media_transport=relay.media_transport(),
        rng=lambda: 0.0,
        **kw,  # pyright: ignore[reportArgumentType]
    )


def _build(env: CaseEnv) -> Adapter:
    return build_adapter(env.wire, env.clock)


async def _pump(env: CaseEnv, adapter: Adapter) -> None:
    env.wire.store_inbound()
    for _ in range(7):  # five poll sweeps bring the next DM discovery
        await env.clock.advance_and_settle(1.0)


BUZZ_CASE = register_case(
    AdapterCase(
        "buzz",
        _env,
        _build,
        user_id=USER,
        group_id=GROUP,
        thread_id=THREAD,
        bot_id=BOT,
        pump=_pump,
        # Buzz edits are not chat events we read (kind 9 only).
        lacks=frozenset({"edit"}),
    )
)
