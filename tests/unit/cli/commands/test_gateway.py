"""P15-28: ``indus gateway`` start / stop / restart / status / install / uninstall / mark-stop.

The gateway is the real one (``indus gateway run`` as a child process) over the spool plugin
platform (``tests/fixtures/gateway/spool_plugin``); a fake ``systemctl`` on the scope's PATH
records what the commands ask of systemd and starts / stops the same real gateway.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import click
import pytest

from indus.cli.commands import gateway as gateway_cmd
from indus.foundation.fsio import atomic_write_json
from indus.foundation.scope import Scope
from indus.service import gateway_control
from indus.service.gateway_units import home_hash
from indus.state import gateway_runtime
from tests.unit.cli.commands.cmdrun import Ran, run_cli

ROOT = Path(__file__).resolve().parents[4]
PLUGIN = ROOT / "tests" / "fixtures" / "gateway" / "spool_plugin"

FAKE_SYSTEMCTL = """#!/bin/sh
echo "$*" >> "$FAKE_SD/log"
[ "$1" = "--user" ] && shift
pid_of() { cat "$FAKE_SD/pid" 2>/dev/null; }
launch() {
  INDUS_HOME="$FAKE_HOME" "$FAKE_PY" -m indus gateway run >/dev/null 2>&1 &
  echo $! > "$FAKE_SD/pid"
  echo active > "$FAKE_SD/state"
}
halt() {
  if [ -f "$FAKE_HOME/gateway_planned_stop.json" ]; then
    echo "planned-stop-present" >> "$FAKE_SD/log"
  fi
  p=$(pid_of)
  if [ -n "$p" ]; then
    kill -TERM "$p" 2>/dev/null
    n=0
    while kill -0 "$p" 2>/dev/null && [ $n -lt 100 ]; do sleep 0.1; n=$((n+1)); done
  fi
  rm -f "$FAKE_SD/pid"
  echo inactive > "$FAKE_SD/state"
}
case "$1" in
  start) launch ;;
  stop) halt ;;
  restart) halt; launch ;;
  show)
    echo "ActiveState=$(cat "$FAKE_SD/state" 2>/dev/null || echo inactive)"
    echo "SubState=running"
    echo "MainPID=$(pid_of || echo 0)"
    echo "Result=success" ;;
esac
exit 0
"""


@pytest.fixture
def gw(scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Scope]:
    """The ``work`` profile with a private PATH, HOME and XDG config dir, on "Linux"."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_sd = tmp_path / "fake-sd"
    fake_sd.mkdir()
    overlay = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "user-home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "FAKE_SD": str(fake_sd),
        "FAKE_PY": sys.executable,
        "FAKE_HOME": str(scope.home),
        "INDUS_SPOOL_ALLOWED_USERS": "200",
        "INDUS_GATEWAY_LOCK_DIR": str(tmp_path / "token-locks"),
        "INDUS_GATEWAY_SUPERVISED": None,
        "NOTIFY_SOCKET": None,
    }
    spool = tmp_path / "spool"
    (spool / "inbound").mkdir(parents=True)
    shutil.copytree(PLUGIN, scope.home / "plugins" / "spool")
    (scope.home / "config.yaml").write_text(
        "model:\n"
        "  default: fake-model\n"
        "  provider: custom\n"
        "  base_url: http://127.0.0.1:9/v1\n"  # never called: no message is sent
        "plugins:\n"
        "  enabled: [spool]\n"
        "platforms:\n"
        "  spool:\n"
        "    enabled: true\n"
        f"    dir: {json.dumps(str(spool))}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_cmd, "PLATFORM", "linux")
    gw_scope = scope.with_env(scope.env.with_overlay(overlay))
    try:
        yield gw_scope
    finally:
        _reap(gw_scope.home, fake_sd)


def _reap(home: Path, fake_sd: Path) -> None:
    """No gateway outlives its test."""
    pids: set[int] = set()
    found = gateway_control.identify(home, timeout=0.5)
    if found is not None:
        pids.add(found["pid"])
    view = gateway_runtime.runtime_view(home)
    if view.pid is not None and view.liveness == "running":
        pids.add(view.pid)
    with contextlib.suppress(OSError, ValueError):
        pids.add(int((fake_sd / "pid").read_text().strip()))
    for pid in pids:
        if pid != os.getpid():
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


def fake_systemctl(scope: Scope) -> Path:
    path = Path(scope.env["PATH"].split(":")[0]) / "systemctl"
    path.write_text(FAKE_SYSTEMCTL)
    path.chmod(0o755)
    return path


def sd_log(scope: Scope) -> list[str]:
    log = Path(scope.env["FAKE_SD"]) / "log"
    return log.read_text().splitlines() if log.exists() else []


def gateway(capsys: pytest.CaptureFixture[str], scope: Scope, *args: str) -> Ran:
    return run_cli(capsys, scope, "gateway", *args)


def unit_path(scope: Scope) -> Path:
    return (
        Path(scope.env["XDG_CONFIG_HOME"])
        / "systemd"
        / "user"
        / f"indus-gateway-{home_hash(scope.home)}.service"
    )


# -- the group ------------------------------------------------------------------------------------


def test_help_lists_the_commands_without_loading_run(
    capsys: pytest.CaptureFixture[str], gw: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_module = gateway_cmd.RUN_TARGET.split(":")[0]
    monkeypatch.delitem(sys.modules, run_module, raising=False)  # an earlier test may load it
    monkeypatch.delitem(gateway_cmd.gateway_group.commands, "run", raising=False)
    ran = gateway(capsys, gw, "--help")
    assert ran.code == 0
    for name in ("run", "start", "stop", "restart", "status", "install", "uninstall"):
        assert f"  {name}" in ran.out
    assert "mark-stop" not in ran.out
    assert run_module not in sys.modules


@click.command("run")
@click.option("--replace", is_flag=True)
def echo_run(replace: bool) -> int:
    click.echo(f"echo gateway run (replace={replace})")
    return 0


def test_run_is_loaded_from_its_target(
    capsys: pytest.CaptureFixture[str], gw: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert gateway_cmd.RUN_TARGET == "indus.gateway.daemon.command:run_command"
    group = gateway_cmd.gateway_group
    target = "tests.unit.cli.commands.test_gateway:echo_run"
    monkeypatch.setitem(group._lazy, "run", target)  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.delitem(group.commands, "run", raising=False)
    try:
        ran = gateway(capsys, gw, "run", "--replace")
    finally:
        group.commands.pop("run", None)
    assert ran.code == 0 and "echo gateway run (replace=True)" in ran.out


# -- without a service ----------------------------------------------------------------------------


def test_start_status_stop_without_a_service(capsys: pytest.CaptureFixture[str], gw: Scope) -> None:
    home = gw.home
    assert gateway(capsys, gw, "stop").out.strip() == "The gateway is not running."
    first = gateway(capsys, gw, "status")
    assert first.code == 1 and "not running" in first.out and "not installed" in first.out

    started = gateway(capsys, gw, "start", "--wait", "20")
    assert started.code == 0, started.err
    assert "Started the gateway in the background" in started.out
    found = gateway_control.identify(home)
    assert found is not None and found["pid"] != os.getpid()
    again = gateway(capsys, gw, "start")
    assert again.code == 0 and f"already running (pid {found['pid']})" in again.out

    status = gateway(capsys, gw, "status")
    assert status.code == 0
    assert f"running (pid {found['pid']}, answered on the control socket)" in status.out
    assert "spool" in status.out and "connected" in status.out
    data = json.loads(gateway(capsys, gw, "status", "--json").out)
    assert data["running"] is True and data["source"] == "socket"
    assert data["status"]["answering_pid"] == found["pid"]
    assert data["service"]["installed"] is False

    stopped = gateway(capsys, gw, "stop", "--timeout", "20")
    assert stopped.code == 0, stopped.err
    assert f"Stopped the gateway (pid {found['pid']})" in stopped.out
    # The gateway found the planned-stop record (its pid and start time) when SIGTERM came,
    # labelled its exit with it and cleared it.
    final = gateway_runtime.read_status(home)
    assert final is not None and final.pid == found["pid"] and final.state == "stopped"
    assert final.exit_reason == "planned_stop:stop"
    assert gateway_runtime.read_planned_stop(home) is None
    after = gateway(capsys, gw, "status")
    assert after.code == 1 and "stopped" in after.out
    log = gateway_runtime.stdio_log_path(home)
    assert log.parent.is_dir()


def test_start_reports_an_early_exit(
    capsys: pytest.CaptureFixture[str], gw: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gateway_cmd, "run_argv", lambda _s: [sys.executable, "-c", "raise SystemExit(78)"]
    )
    ran = gateway(capsys, gw, "start", "--wait", "20")
    assert ran.code == 1
    assert "exited (code 78) before it answered" in ran.err
    assert str(gateway_runtime.stdio_log_path(gw.home)) in ran.err


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_status_on_a_dead_pid_with_a_fresh_file_is_stale(
    capsys: pytest.CaptureFixture[str], gw: Scope
) -> None:
    atomic_write_json(
        gw.home / gateway_runtime.STATUS_FILE,
        {"state": "running", "pid": _dead_pid(), "start_time": 1, "updated_at": time.time()},
    )
    ran = gateway(capsys, gw, "status")
    assert ran.code == 1 and "stale status (its process is gone)" in ran.out
    stop = gateway(capsys, gw, "stop")
    assert stop.code == 0 and "stale" in stop.out
    assert gateway_runtime.read_planned_stop(gw.home) is None  # nothing was signalled


def test_status_file_alone_counts_while_fresh(
    capsys: pytest.CaptureFixture[str], gw: Scope
) -> None:
    me = os.getpid()
    payload = {
        "state": "degraded",
        "pid": me,
        "start_time": gateway_runtime.process_start_time(me),
        "updated_at": time.time(),
        "platforms": {"slack": {"state": "retrying", "needs_attention": True}},
    }
    atomic_write_json(gw.home / gateway_runtime.STATUS_FILE, payload)
    ran = gateway(capsys, gw, "status")
    assert ran.code == 0 and "degraded" in ran.out and "status file" in ran.out
    assert "slack" in ran.out and "needs attention" in ran.out
    payload["updated_at"] = time.time() - 121
    atomic_write_json(gw.home / gateway_runtime.STATUS_FILE, payload)
    old = gateway(capsys, gw, "status")
    assert old.code == 1 and "no heartbeat for over 120 s" in old.out


def test_a_reused_pid_is_never_signalled(gw: Scope) -> None:
    me = os.getpid()
    start = gateway_runtime.process_start_time(me)
    assert start is not None
    assert gateway_runtime.alive(me, start)
    assert not gateway_runtime.alive(me, start + 1)  # same pid, another process
    assert not gateway_runtime.alive(_dead_pid(), None)


# -- with a systemd user unit (fake systemctl) ----------------------------------------------------


def test_install_writes_a_user_unit_and_enables_it(
    capsys: pytest.CaptureFixture[str], gw: Scope
) -> None:
    fake_systemctl(gw)
    ran = gateway(capsys, gw, "install")
    assert ran.code == 0, ran.err
    path = unit_path(gw)
    assert path.is_file() and f"Installed the service {path.name}" in ran.out
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    text = path.read_text()
    assert f'Environment="INDUS_HOME={gw.home}"' in text and "RestartPreventExitStatus=78" in text
    assert sd_log(gw) == ["--user daemon-reload", f"--user enable {path.name}"]
    assert "loginctl enable-linger" in ran.out and "indus gateway start" in ran.out

    again = gateway(capsys, gw, "install")
    assert again.code == 0 and "already installed" in again.out
    path.write_text(text + "# edited\n")
    conflict = gateway(capsys, gw, "install")
    assert conflict.code == 1 and "--force" in conflict.err
    assert gateway(capsys, gw, "install", "--force").code == 0
    assert path.read_text() == text


def test_service_start_stop_restart_status_uninstall(
    capsys: pytest.CaptureFixture[str], gw: Scope
) -> None:
    fake_systemctl(gw)
    assert gateway(capsys, gw, "install").code == 0
    name = unit_path(gw).name

    started = gateway(capsys, gw, "start", "--wait", "20")
    assert started.code == 0, started.err
    assert f"through its service {name}" in started.out
    assert f"--user start {name}" in sd_log(gw)
    first = gateway_control.identify(gw.home)
    assert first is not None

    status = gateway(capsys, gw, "status")
    assert status.code == 0 and f"Service: {name} — active, pid {first['pid']}" in status.out

    restarted = gateway(capsys, gw, "restart", "--wait", "20")
    assert restarted.code == 0, restarted.err
    second = gateway_control.identify(gw.home)
    assert second is not None and second["pid"] != first["pid"]
    log = sd_log(gw)
    assert f"--user restart {name}" in log
    assert log[log.index(f"--user restart {name}") + 1] == "planned-stop-present"

    stopped = gateway(capsys, gw, "stop", "--timeout", "20")
    assert stopped.code == 0, stopped.err
    log = sd_log(gw)
    assert log[-2:] == [f"--user stop {name}", "planned-stop-present"]
    final = gateway_runtime.read_status(gw.home)
    assert final is not None and final.pid == second["pid"]
    assert final.exit_reason == "planned_stop:stop"

    removed = gateway(capsys, gw, "uninstall")
    assert removed.code == 0 and not unit_path(gw).exists()
    assert f"--user disable {name}" in sd_log(gw)
    assert gateway(capsys, gw, "uninstall").out.strip().startswith("No gateway service")


def test_install_without_systemctl(capsys: pytest.CaptureFixture[str], gw: Scope) -> None:
    gw2 = gw.with_env(gw.env.with_overlay({"PATH": str(Path(gw.env["PATH"].split(":")[0]))}))
    ran = gateway(capsys, gw2, "install")
    assert ran.code == 1 and "`systemctl` was not found" in ran.err
    assert not unit_path(gw).exists()


def test_install_system_prints_and_writes_nothing(
    capsys: pytest.CaptureFixture[str], gw: Scope
) -> None:
    fake_systemctl(gw)
    ran = gateway(capsys, gw, "install", "--system")
    assert ran.code == 0
    assert "User=" in ran.out and "WantedBy=multi-user.target" in ran.out
    assert "sudo tee /etc/systemd/system/" in ran.out
    assert sd_log(gw) == [] and not unit_path(gw).exists()


def test_windows_has_no_native_service(
    capsys: pytest.CaptureFixture[str], gw: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway_cmd, "PLATFORM", "win32")
    ran = gateway(capsys, gw, "install")
    assert ran.code == 1 and "WSL2" in ran.out


def test_mark_stop_records_a_planned_stop_for_the_unit(
    capsys: pytest.CaptureFixture[str], gw: Scope
) -> None:
    me = os.getpid()
    ran = gateway(capsys, gw, "mark-stop", "--pid", str(me))
    assert ran.code == 0
    record = gateway_runtime.read_planned_stop(gw.home)
    assert record is not None and record.by == "unit"
    assert record.matches(me, gateway_runtime.process_start_time(me))
    assert not record.matches(me + 1, None)
    mode = stat.S_IMODE((gw.home / gateway_runtime.PLANNED_STOP_FILE).stat().st_mode)
    assert mode == 0o600
