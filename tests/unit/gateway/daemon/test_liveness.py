"""P15-24: who counts as the live gateway of a profile (R06.18, R03.20) and the instance lock."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from indus.gateway.daemon.liveness import InstanceLock, claim_instance, release_instance
from indus.state.gateway_runtime import (
    PidRecord,
    PlatformStatus,
    ProcessIdentity,
    RuntimeStatus,
    clear_planned_stop,
    is_stale,
    live_gateway,
    lock_path,
    pid_alive,
    pid_namespace,
    pid_path,
    process_start_time,
    read_pid_record,
    read_planned_stop,
    read_status,
    record_planned_stop,
    supervised,
    write_pid_record,
    write_status,
)
from tests.fakes.pids import FakePids

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX locks")
ROOT = Path(__file__).resolve().parents[4]


def probes(table: FakePids, *, ns: str | None = "4026531836", host: str = "box") -> dict[str, Any]:
    def start(pid: int) -> int | None:
        found = table.fingerprint(pid)
        return None if found is None else int(found.split(":")[1])

    return {
        "alive": table.exists,
        "start_time": start,
        "namespace": lambda: ns,
        "host": lambda: host,
    }


def ident(pid: int, start: int | None, ns: str | None = "4026531836") -> ProcessIdentity:
    return ProcessIdentity(pid, start, ns, "box")


# -- process identity ------------------------------------------------------------------------------


def test_a_recycled_pid_with_another_start_time_is_not_alive() -> None:
    table = FakePids().spawn(300, "boot:111")
    assert ident(300, 111).is_alive(**probes(table))
    table.reuse(300, "boot:222")
    assert not ident(300, 111).is_alive(**probes(table))
    table.kill(300)
    assert not ident(300, 111).is_alive(**probes(table))


def test_unknowns_fail_closed_and_foreign_records_are_not_probed() -> None:
    table = FakePids().spawn(300, "boot:111")
    table.unreadable(300)
    assert ident(300, 111).is_alive(**probes(table))  # start time unreadable: alive
    assert ident(300, None).is_alive(**probes(table))  # nothing recorded: alive while pid lives
    # A record from another pid namespace (another container) or host cannot be probed here.
    assert ident(999, 1, ns="1234").is_alive(**probes(table))
    elsewhere = ProcessIdentity(999, 1, None, "other-host")
    assert elsewhere.is_alive(**probes(table))
    # Same namespace, pid gone: dead.
    assert not ident(999, 1).is_alive(**probes(table))
    assert not ProcessIdentity(0).is_alive()


def test_real_process_identity() -> None:
    me = ProcessIdentity.current()
    assert me.pid == os.getpid() and pid_alive(me.pid)
    assert me.is_alive()
    assert not pid_alive(0) and not pid_alive(-3)
    if sys.platform.startswith("linux"):
        assert me.start_time == process_start_time(os.getpid())
        assert me.pidns is not None and me.pidns == pid_namespace()
        assert not ProcessIdentity(me.pid, (me.start_time or 0) + 1, me.pidns, me.host).is_alive()


# -- records ---------------------------------------------------------------------------------------


def test_pid_record_round_trip_and_live_gateway(tmp_path: Path) -> None:
    record = PidRecord(ProcessIdentity.current(), home=str(tmp_path), profile="work")
    write_pid_record(tmp_path, record)
    assert pid_path(tmp_path).stat().st_mode & 0o777 == 0o600
    assert read_pid_record(tmp_path) == record
    assert live_gateway(tmp_path) == record
    table = FakePids()  # our pid is not in this table: the recorded process is gone
    here = probes(table, ns=record.process.pidns, host=record.process.host)
    assert live_gateway(tmp_path, **here) is None
    pid_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert read_pid_record(tmp_path) is None and live_gateway(tmp_path) is None
    pid_path(tmp_path).write_text('{"pid": true}', encoding="utf-8")
    assert read_pid_record(tmp_path) is None


def test_status_round_trip_and_staleness(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    status = RuntimeStatus(
        "running",
        "",
        ProcessIdentity(42, 7, "1", "box"),
        profile="work",
        code_version="0.0.1",
        epoch="e",
        platforms={
            "telegram": PlatformStatus("connected"),
            "slack": PlatformStatus("retrying", True, "connect_timeout", "no", "2026-01-01"),
        },
    ).stamped(now)
    write_status(tmp_path, status)
    assert (tmp_path / "gateway_state.json").stat().st_mode & 0o777 == 0o600
    back = read_status(tmp_path)
    assert back == status
    assert not is_stale(back, now + timedelta(seconds=120))
    assert is_stale(back, now + timedelta(seconds=121))
    assert is_stale(None, now)
    garbled = RuntimeStatus("running", "yesterday", ProcessIdentity(1))
    assert is_stale(garbled, now)
    (tmp_path / "gateway_state.json").write_text('{"state": "flying", "pid": 3}')
    assert read_status(tmp_path) is None


def test_planned_stop_record_and_supervised_flag(tmp_path: Path) -> None:
    assert read_planned_stop(tmp_path) is None
    record_planned_stop(tmp_path, 4242, "update")
    found = read_planned_stop(tmp_path)
    assert found is not None and found.pid == 4242 and found.reason == "update"
    clear_planned_stop(tmp_path)
    clear_planned_stop(tmp_path)
    assert read_planned_stop(tmp_path) is None
    assert supervised({"INDUS_GATEWAY_SUPERVISED": "1"})
    assert not supervised({"INDUS_GATEWAY_SUPERVISED": "0"}) and not supervised({})


# -- the instance lock -----------------------------------------------------------------------------

HOLDER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from indus.gateway.daemon.liveness import claim_instance
    from indus.state.gateway_runtime import PidRecord, ProcessIdentity
    home = Path(sys.argv[1])
    claim = claim_instance(home, PidRecord(ProcessIdentity.current(), home=str(home)))
    print(claim.outcome, flush=True)
    sys.stdin.read()
    """
)


def _holder(home: Path) -> subprocess.Popen[str]:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    child = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(home)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "acquired"
    return child


def test_the_lock_excludes_another_process_and_dies_with_it(tmp_path: Path) -> None:
    child = _holder(tmp_path)
    try:
        record = PidRecord(ProcessIdentity.current(), home=str(tmp_path))
        claim = claim_instance(tmp_path, record, settle=0.2)
        assert claim.outcome == "running"
        assert claim.holder is not None and claim.holder.pid == child.pid
        assert live_gateway(tmp_path) == claim.holder
    finally:
        child.kill()
        child.wait()
    # The kernel dropped the dead holder's lock; its pid record is stale and gets replaced.
    claim = claim_instance(tmp_path, record)
    assert claim.outcome == "acquired"
    assert read_pid_record(tmp_path) == record
    release_instance(tmp_path, claim, record.process)
    assert not pid_path(tmp_path).exists()
    assert lock_path(tmp_path).exists()  # the lock file itself is never deleted


def test_replace_terminates_the_holder_and_takes_over(tmp_path: Path) -> None:
    child = _holder(tmp_path)
    try:
        record = PidRecord(ProcessIdentity.current(), home=str(tmp_path))
        claim = claim_instance(tmp_path, record, replace=True, wait=10.0)
        assert claim.outcome == "acquired" and claim.replaced
        assert child.wait(10) == -signal.SIGTERM
        release_instance(tmp_path, claim, record.process)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_replace_never_signals_a_recycled_pid(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path)
    assert lock.try_acquire()  # held by "someone" for the whole test
    try:
        table = FakePids().spawn(777, "boot:1")
        write_pid_record(tmp_path, PidRecord(ProcessIdentity(777, 1, None, "")))
        table.reuse(777, "boot:2")  # same number, another process now
        sent: list[tuple[int, int]] = []
        me = PidRecord(ProcessIdentity.current())
        claim = claim_instance(
            tmp_path,
            me,
            replace=True,
            wait=0.2,
            kill_wait=0.2,
            settle=0.0,
            sleep=lambda _s: None,
            kill=lambda pid, sig: sent.append((pid, sig)),
            start_time=lambda pid: 2 if pid == 777 else None,
            probes={"alive": table.exists, "start_time": lambda pid: 2, "namespace": lambda: None},
        )
        assert claim.outcome == "running" and sent == []
    finally:
        lock.release()


def test_kill_follows_term_only_while_the_start_time_matches(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path)
    assert lock.try_acquire()
    try:
        write_pid_record(tmp_path, PidRecord(ProcessIdentity(778, 5, None, "")))
        sent: list[int] = []
        claim = claim_instance(
            tmp_path,
            PidRecord(ProcessIdentity.current()),
            replace=True,
            wait=0.05,
            kill_wait=0.05,
            settle=0.0,
            sleep=lambda _s: None,
            kill=lambda pid, sig: sent.append(sig),
            start_time=lambda pid: 5,
            probes={
                "alive": lambda pid: True,
                "start_time": lambda pid: 5,
                "namespace": lambda: None,
            },
        )
        assert claim.outcome == "running"  # our fake holder ignores both signals
        assert sent == [signal.SIGTERM, signal.SIGKILL]
    finally:
        lock.release()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc process states")
def test_an_unreaped_zombie_counts_as_gone() -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        stat = Path(f"/proc/{child.pid}/stat")
        end = threading.Event()
        for _ in range(200):  # until it has exited (not yet reaped: we never wait on it)
            if stat.read_text(encoding="ascii").rsplit(")", 1)[-1].split()[0] == "Z":
                break
            end.wait(0.05)
        assert not pid_alive(child.pid)
        assert not ProcessIdentity(child.pid, process_start_time(child.pid)).is_alive()
    finally:
        child.wait()
