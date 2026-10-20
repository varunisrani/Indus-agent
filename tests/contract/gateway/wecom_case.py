"""The WeCom smart-robot adapter's contract case (P16-17): the real adapter over its wire fake."""

from __future__ import annotations

from indus.gateway.adapters.base import Adapter
from indus.gateway.adapters.wecom.adapter import WeComOptions
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.wecom_wire import WeComWire


def _make_env(clock: FakeClock) -> CaseEnv:
    wire = WeComWire(clock=clock)
    return CaseEnv(clock, wire.platform, wire)


def _build(env: CaseEnv) -> Adapter:
    return env.wire.adapter(options=WeComOptions(group_policy="open"))


WECOM_CASE = register_case(
    AdapterCase(
        "wecom",
        _make_env,
        _build,
        # No threads, edit events or bot flags; voice notes arrive as WeCom's transcript.
        lacks=frozenset({"thread", "bot", "edit", "voice_media"}),
    )
)
