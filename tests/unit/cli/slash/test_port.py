"""``HostPort``: the slash commands' session port over a real in-process host (P06-09)."""

from __future__ import annotations

from pathlib import Path

from indus.cli.slash.dispatch import dispatch
from indus.cli.slash.port import HostPort
from indus.foundation.scope import Scope
from indus.protocol.methods import SessionCreateParams
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn
from tests.unit.cli.slash._fakes import make_ctx
from tests.unit.service.test_host_inprocess import host_for


def test_host_port_drives_a_real_session(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    host = host_for(scope, fake_clock, tmp_path, text_turn("hello"))
    with host:
        port = HostPort(host, scope)
        sid = port.create(SessionCreateParams())
        info = port.route(sid)
        assert info.provider == "fake" and info.context_length > 0
        assert port.wait_idle(sid, 1.0)  # no turn yet
        assert not port.interrupt(sid)
        assert not port.steer(sid, "nothing runs")
        assert isinstance(port.tool_names(sid), list)
        assert port.stop_processes(sid, all_jobs=True) == 0
        assert port.stop_processes(sid, all_jobs=False) == 0

        switched = port.switch_route(sid, info.route, effort="high", keep_effort=False)
        assert switched.requested_effort == "high"
        kept = port.switch_route(sid, info.route, keep_effort=True)
        assert kept.requested_effort == "high"

        ctx = make_ctx(scope, port=port, session_id=sid)
        dispatch("/model", ctx)
        assert ctx.lines[0].startswith(f"Model: {info.model} (provider fake")
        result = dispatch("/new now", ctx)
        assert result.switch_to is not None and result.switch_to != sid
        assert host.service.attached() == [result.switch_to]
