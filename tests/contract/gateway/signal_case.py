"""The Signal adapter in the shared contract suite, over the fake signal-cli daemon (P16-05)."""

from __future__ import annotations

from indus.gateway.adapters.base import Adapter, utf16_units
from indus.gateway.adapters.signal.adapter import SignalAdapter
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.signal_wire import ACCOUNT, MAX_LEN, SignalDaemon

DAEMON_URL = "http://127.0.0.1:8080"


def _env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform(
        "signal", clock=clock, max_len=MAX_LEN, len_fn=utf16_units, bot_user_id=ACCOUNT
    )
    daemon = SignalDaemon(platform)
    return CaseEnv(clock, platform, daemon)


def _build(env: CaseEnv) -> Adapter:
    daemon: SignalDaemon = env.wire
    return SignalAdapter(
        clock=env.clock,
        http_url=DAEMON_URL,
        account=daemon.account,
        group_chats=("*",),
        transport=daemon.transport,
        rng=lambda: 0.0,
    )


SIGNAL_CASE = register_case(
    AdapterCase(
        "signal",
        _env,
        _build,
        user_id="+15550000200",
        group_id="group.R3JvdXAtMzAw",
        thread_id="400",
        bot_id="+15550000500",
        # Signal has no threads and does not mark bot accounts.
        lacks=frozenset({"thread", "bot"}),
    )
)
