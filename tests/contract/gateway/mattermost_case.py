"""The Mattermost adapter's contract case (P16-08): the real adapter over its wire fake."""

from __future__ import annotations

from indus.gateway.adapters.base import Adapter
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.mattermost_wire import MattermostWire


def _make_env(clock: FakeClock) -> CaseEnv:
    wire = MattermostWire(clock=clock)
    return CaseEnv(clock, wire.platform, wire)


def _build(env: CaseEnv) -> Adapter:
    return env.wire.adapter()


MATTERMOST_CASE = register_case(AdapterCase("mattermost", _make_env, _build))
