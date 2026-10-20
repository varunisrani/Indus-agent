"""The DingTalk adapter's contract case (P16-16): the real adapter over its wire fake."""

from __future__ import annotations

from indus.gateway.adapters.base import Adapter
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.dingtalk_wire import DingTalkWire


def _make_env(clock: FakeClock) -> CaseEnv:
    wire = DingTalkWire(clock=clock)
    return CaseEnv(clock, wire.platform, wire)


def _build(env: CaseEnv) -> Adapter:
    return env.wire.adapter()


DINGTALK_CASE = register_case(
    AdapterCase(
        "dingtalk",
        _make_env,
        _build,
        # DingTalk robots see no threads, no edits and no bot flag on senders.
        lacks=frozenset({"thread", "bot", "edit"}),
    )
)
