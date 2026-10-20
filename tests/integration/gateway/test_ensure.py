"""P17-15: ``ensure`` and ``indus gateway ensure`` against real daemons.

Each test runs real ``indus gateway run`` processes for a temp profile home (no platform enabled:
the daemon keeps running for its background parts). Nothing outlives its test: every daemon the
home ever answered for is killed at teardown.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import indus
from indus.foundation.scope import Scope
from indus.service import gateway_control
from indus.service.gateway_ensure import (
    AUTOSTART_ENV,
    RecoverBudget,
    daemon_argv,
    ensure,
    planned_stop_reason,
)
from indus.service.ws_auth import stable_profile_id
from indus.state import gateway_runtime
from tests.unit.cli.commands.cmdrun import run_cli

OLD = "0.0.0-old"
_STALE = (
    "import sys, indus\n"
    f"indus.__version__ = {OLD!r}\n"
    "from indus.cli.entry import main\n"
    "raise SystemExit(main(['gateway', 'run']))\n"
)


class Pids:
    def __init__(self) -> None:
        self.seen: set[int] = set()

    def note(self, pid: int | None) -> None:
        if pid is not None:
            self.seen.add(pid)


@pytest.fixture
def pids() -> Pids:
    return Pids()


@pytest.fixture
def gw(scope: Scope, tmp_path: Path, pids: Pids) -> Iterator[Scope]:
    """The ``work`` profile with a private PATH and HOME, no platform, model never called."""
    overlay = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "user-home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "INDUS_GATEWAY_LOCK_DIR": str(tmp_path / "token-locks"),
        "INDUS_GATEWAY_SUPERVISED": None,
        "NOTIFY_SOCKET": None,
        "INDUS_YOLO_MODE": "1",  # a launch option: must not reach the daemon
        "INDUS_TUI_QUERY": "hello",  # one TUI run's value: must not reach it either
        "INDUS_MODEL": "launch-model",
    }
    (scope.home / "config.yaml").write_text(
        "model:\n  default: fake-model\n  provider: custom\n  base_url: http://127.0.0.1:9/v1\n",
        encoding="utf-8",
    )
    scope.home.chmod(0o700)
    gw_scope = scope.with_env(scope.env.with_overlay(overlay))
    try:
        yield gw_scope
    finally:
        _reap(gw_scope.home, pids)


def _reap(home: Path, pids: Pids) -> None:
    found = gateway_control.identify(home, timeout=0.5)
    if found is not None:
        pids.note(found["pid"])
    view = gateway_runtime.runtime_view(home)
    if view.liveness == "running":
        pids.note(view.pid)
    for pid in pids.seen:
        if pid != os.getpid():
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


def env_of(scope: Scope) -> dict[str, str]:
    return scope.env.to_dict()


def wait_gone(pid: int, timeout: float = 30.0) -> None:
    end = time.monotonic() + timeout
    while gateway_runtime.pid_alive(pid):
        assert time.monotonic() < end, f"pid {pid} is still running"
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        gateway_runtime_pause()


def gateway_runtime_pause() -> None:
    import threading

    threading.Event().wait(0.1)


# -- start / warm ---------------------------------------------------------------------------------


def test_cold_start_then_warm_ensure_does_not_spawn(gw: Scope, pids: Pids) -> None:
    home = gw.home
    first = ensure(home, env_of(gw), timeout=60)
    pids.note(first.pid)
    assert first.state == "ready", first
    assert first.started and not first.restarted and first.pid is not None
    endpoint = first.endpoint
    assert endpoint is not None
    assert endpoint.profile_id == stable_profile_id(home) and endpoint.instance_id
    assert endpoint.code_version == indus.__version__
    # The control socket answers before the WebSocket is mounted: ensure waits for the endpoint.
    assert endpoint.ws_url is not None and gateway_control.ws_url_is_safe(endpoint.ws_url)
    assert set(first.to_json()) == {"ws_url", "profile_id", "instance_id", "code_version"}

    second = ensure(home, env_of(gw), timeout=60)
    assert second.state == "ready" and not second.started and second.pid == first.pid
    assert second.endpoint == endpoint


def test_the_started_daemon_is_detached_and_logs_to_the_stdio_log(gw: Scope, pids: Pids) -> None:
    got = ensure(gw.home, env_of(gw), timeout=60)
    pids.note(got.pid)
    assert got.state == "ready" and got.pid is not None
    pid = got.pid
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)  # double fork: the daemon is not our child
    assert os.getsid(pid) == pid != os.getsid(0)  # its own session
    environ = Path(f"/proc/{pid}/environ")
    if environ.exists():
        values = dict(
            item.split("=", 1)
            for item in environ.read_text(errors="replace").split("\0")
            if "=" in item
        )
        assert values.get(AUTOSTART_ENV) == "1"
        for name in ("INDUS_YOLO_MODE", "INDUS_TUI_QUERY", "INDUS_MODEL"):
            assert name not in values  # launch options belong to a session or a run
        assert values.get("INDUS_HOME") == str(gw.home)
    assert gateway_runtime.stdio_log_path(gw.home).is_file()


def test_version_skew_restarts_once_then_succeeds(gw: Scope, pids: Pids) -> None:
    stale_argv = [sys.executable, "-c", _STALE]
    old = ensure(gw.home, env_of(gw), timeout=60, version=OLD, argv=stale_argv)
    pids.note(old.pid)
    assert old.state == "ready" and old.endpoint is not None
    assert old.endpoint.code_version == OLD
    looked = ensure(gw.home, env_of(gw), mode="discover", timeout=5)
    assert looked.state == "ready" and looked.skew and looked.pid == old.pid  # only reported

    fresh = ensure(gw.home, env_of(gw), timeout=90)
    pids.note(fresh.pid)
    assert fresh.state == "ready", fresh
    assert fresh.restarted and fresh.pid != old.pid and not fresh.skew
    assert fresh.endpoint is not None and fresh.endpoint.code_version == indus.__version__
    assert old.pid is not None and not gateway_runtime.pid_alive(old.pid)
    again = ensure(gw.home, env_of(gw), timeout=30)
    assert again.pid == fresh.pid and not again.restarted


# -- planned stop and recovery --------------------------------------------------------------------


def _stop_on_purpose(scope: Scope, pid: int, start_time: int | None) -> None:
    gateway_runtime.record_planned_stop(scope.home, pid, "stop", start_time=start_time, by="cli")
    os.kill(pid, signal.SIGTERM)
    wait_gone(pid)


def test_a_planned_stop_is_respected_by_discovery_and_recovery(gw: Scope, pids: Pids) -> None:
    home = gw.home
    first = ensure(home, env_of(gw), timeout=60)
    pids.note(first.pid)
    assert first.state == "ready" and first.pid is not None
    found = gateway_control.identify(home)
    assert found is not None
    _stop_on_purpose(gw, first.pid, found.get("start_time"))
    assert planned_stop_reason(home) == "stop"

    looked = ensure(home, env_of(gw), mode="discover", timeout=5)
    assert looked.state == "absent" and not looked.started
    recovered = ensure(home, env_of(gw), mode="recover", budget=RecoverBudget(), timeout=5)
    assert recovered.state == "stopped" and recovered.terminal
    assert gateway_control.identify(home) is None

    started = ensure(home, env_of(gw), mode="start", timeout=60)
    pids.note(started.pid)
    assert started.state == "ready" and started.started and started.pid != first.pid


def test_a_crashed_daemon_is_recovered_within_the_budget(gw: Scope, pids: Pids) -> None:
    home = gw.home
    first = ensure(home, env_of(gw), timeout=60)
    pids.note(first.pid)
    assert first.pid is not None
    os.kill(first.pid, signal.SIGKILL)
    wait_gone(first.pid)
    assert planned_stop_reason(home) is None
    budget = RecoverBudget()
    back = ensure(home, env_of(gw), mode="recover", budget=budget, timeout=60)
    pids.note(back.pid)
    assert back.state == "ready" and back.started and back.pid != first.pid


def test_a_fourth_recover_within_60_s_is_refused(gw: Scope, tmp_path: Path) -> None:
    now = [100.0]
    budget = RecoverBudget(monotonic=lambda: now[0])
    taken = []
    for _ in range(3):
        taken.append(budget.take())
        now[0] += 20.0
    assert taken == [True, True, True]  # at 100, 120 and 140; now 160
    now[0] -= 0.1
    marker = tmp_path / "spawned"
    argv = [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
    refused = ensure(gw.home, env_of(gw), mode="recover", budget=budget, argv=argv, timeout=2)
    assert refused.state == "refused" and refused.reason == "recover_budget"
    assert "3 times in 60 s" in refused.detail and not refused.terminal
    assert not marker.exists()
    assert not budget.available()
    now[0] += 0.2  # the first recovery (at 100) left the window
    assert budget.available() and budget.take() and not budget.take()


# -- failures -------------------------------------------------------------------------------------


def test_an_early_exit_is_reported_with_the_log(gw: Scope, pids: Pids) -> None:
    argv = [sys.executable, "-c", "import sys; print('bad config'); sys.exit(78)"]
    got = ensure(gw.home, env_of(gw), argv=argv, timeout=30)
    pids.note(got.pid)
    assert got.state == "exited" and got.started
    assert str(gateway_runtime.stdio_log_path(gw.home)) in got.detail
    assert "bad config" in gateway_runtime.stdio_log_path(gw.home).read_text()
    assert "  bad config" in got.detail  # this launch's output, shown to the person


def test_a_starting_daemon_is_waited_for_not_doubled(gw: Scope, tmp_path: Path) -> None:
    gateway_runtime.write_status(
        gw.home,
        gateway_runtime.RuntimeStatus(
            state="starting",
            updated_at=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            process=gateway_runtime.ProcessIdentity.current(),
        ),
    )
    marker = tmp_path / "spawned"
    argv = [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
    looked = ensure(gw.home, env_of(gw), mode="discover", argv=argv, timeout=1)
    assert looked.state == "starting"
    waited = ensure(gw.home, env_of(gw), argv=argv, timeout=0.5)
    assert waited.state == "starting" and waited.reason == "timeout" and not marker.exists()


def test_an_unsafe_home_is_refused(gw: Scope) -> None:
    gw.home.chmod(0o777)
    try:
        got = ensure(gw.home, env_of(gw), timeout=1)
    finally:
        gw.home.chmod(0o700)
    assert got.state == "refused" and got.reason == "unsafe_home" and got.terminal


def test_the_daemon_argv_runs_gateway_run() -> None:
    assert daemon_argv()[-2:] == ["gateway", "run"]


# -- the command ----------------------------------------------------------------------------------


def test_the_command_prints_the_descriptor_and_never_a_ticket(
    capsys: pytest.CaptureFixture[str], gw: Scope, pids: Pids
) -> None:
    missing = run_cli(capsys, gw, "gateway", "ensure", "--json", "--no-start")
    assert missing.code == 1
    assert json.loads(missing.out) == {
        "state": "absent",
        "reason": "not_running",
        "detail": "no gateway runs for this profile",
    }
    plain = run_cli(capsys, gw, "gateway", "ensure", "--no-start")
    assert plain.code == 1 and "No gateway runs" in plain.err

    ran = run_cli(capsys, gw, "gateway", "ensure", "--json", "--timeout", "60")
    found = gateway_control.identify(gw.home)
    pids.note(found["pid"] if found else None)
    assert ran.code == 0, ran.err
    descriptor = json.loads(ran.out)
    assert set(descriptor) == {"ws_url", "profile_id", "instance_id", "code_version"}
    assert descriptor["profile_id"] == stable_profile_id(gw.home)
    assert "ticket" not in ran.out.lower()
    text = run_cli(capsys, gw, "gateway", "ensure")
    assert text.code == 0 and "The gateway is running" in text.out
