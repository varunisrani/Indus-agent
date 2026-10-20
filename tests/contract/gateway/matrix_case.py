"""The Matrix adapter's contract case (P16-06): the real adapter over the Matrix wire fake."""

from __future__ import annotations

from indus.gateway.adapters.base import Adapter
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.matrix_wire import MatrixWire

USER = "@ana:example.org"


def _make_env(clock: FakeClock) -> CaseEnv:
    wire = MatrixWire(clock=clock)
    wire.platform.add_chat(USER, "dm", "Ana")
    return CaseEnv(clock, wire.platform, wire)


def _build(env: CaseEnv) -> Adapter:
    return env.wire.adapter()


MATRIX_CASE = register_case(
    AdapterCase(
        "matrix",
        _make_env,
        _build,
        user_id=USER,
        group_id="!ops:example.org",
        thread_id="$root",
        bot_id="@helper:example.org",
    )
)
