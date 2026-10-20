"""The Photon adapter in the shared contract suite, over the fake sidecar (P16-25)."""

from __future__ import annotations

from indus.gateway.adapters.base import Adapter, code_points
from indus.gateway.adapters.photon.adapter import PhotonAdapter
from tests.contract.gateway.conftest import AdapterCase, CaseEnv, register_case
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.photon_wire import MAX_LEN, FakeLauncher, FakeSidecar


def _env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform("photon", clock=clock, max_len=MAX_LEN, len_fn=code_points)
    return CaseEnv(clock, platform, FakeSidecar(platform))


def _build(env: CaseEnv) -> Adapter:
    sidecar: FakeSidecar = env.wire
    return PhotonAdapter(
        clock=env.clock, launcher=FakeLauncher(sidecar), transport=sidecar.transport
    )


PHOTON_CASE = register_case(
    AdapterCase(
        "photon",
        _env,
        _build,
        user_id="+15550000200",
        group_id="any;+;chat300",
        thread_id="400",
        bot_id="+15550000500",
        # iMessage has no threads and no bot accounts; the sidecar API carries no edits.
        lacks=frozenset({"thread", "bot", "edit"}),
    )
)
