"""Holder stamps and the staleness rule for lock rows (R03.20)."""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

from indus.state import holders


@pytest.fixture
def same_ns(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(holders, "local_pid_namespace", lambda: "4026531836")
    monkeypatch.setattr(holders, "_host", lambda: "box-a")
    return "4026531836"


def stamp(pid: int, ns: str = "4026531836", host: str = "box-a", tail: str = "") -> str:
    return f"pid={pid}:pidns={ns}:{host}{tail}"


def test_stamp_has_pid_namespace_and_host() -> None:
    text = holders.holder_stamp()
    assert re.fullmatch(r"pid=\d+:pidns=(\d+|-):[^:]+", text)
    info = holders.parse_holder(text)
    assert info is not None and info.pid == os.getpid()


def test_parse_accepts_a_suffix_and_rejects_strangers() -> None:
    info = holders.parse_holder("pid=12:pidns=99:host-1:task=abc:n=1")
    assert info == holders.HolderInfo(12, "99", "host-1")
    assert holders.parse_holder("pid=12:pidns=-:h") == holders.HolderInfo(12, None, "h")
    for bad in (None, "", "legacy", "pid=x:pidns=1:h", "pid=1:h", "pid=1:pidns=1:"):
        assert holders.parse_holder(bad) is None


def test_expired_row_is_stale_without_probing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(holders, "_pid_gone", lambda pid: pytest.fail("must not probe"))
    assert holders.holder_is_stale("anything", 100.0, 100.0)
    assert holders.holder_is_stale("anything", 99.0, 100.0)


def test_dead_pid_in_our_namespace_is_stale(same_ns: str, monkeypatch: pytest.MonkeyPatch) -> None:
    probed: list[int] = []

    def gone(pid: int) -> bool:
        probed.append(pid)
        return True

    monkeypatch.setattr(holders, "_pid_gone", gone)
    assert holders.holder_is_stale(stamp(424242, tail=":t=1"), 500.0, 100.0)
    assert probed == [424242]


@pytest.mark.parametrize(
    "holder",
    [
        stamp(424242, ns="4026532999"),  # sibling container
        stamp(424242, ns="-"),  # writer could not read its namespace
        stamp(424242, host="box-b"),  # another machine on a shared volume
        "pid=424242:tid=1",  # not one of our stamps
        "legacy-holder",
    ],
)
def test_holders_we_cannot_judge_wait_for_expiry(
    same_ns: str, monkeypatch: pytest.MonkeyPatch, holder: str
) -> None:
    monkeypatch.setattr(holders, "_pid_gone", lambda pid: pytest.fail("must not probe"))
    assert not holders.holder_is_stale(holder, 500.0, 100.0)
    assert holders.holder_is_stale(holder, 100.0, 100.0)


def test_own_pid_and_live_pid_are_kept(same_ns: str) -> None:
    assert not holders.holder_is_stale(stamp(os.getpid()), 500.0, 100.0)
    assert not holders.holder_is_stale(stamp(os.getppid()), 500.0, 100.0)


def test_probe_doubt_keeps_the_row(same_ns: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(pid: int) -> bool:
        raise RuntimeError("probe failed")

    monkeypatch.setattr(holders, "_pid_gone", broken)
    assert not holders.holder_is_stale(stamp(424242), 500.0, 100.0)


def test_unknown_local_namespace_never_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(holders, "local_pid_namespace", lambda: None)
    monkeypatch.setattr(holders, "_pid_gone", lambda pid: pytest.fail("must not probe"))
    assert not holders.holder_process_is_dead(stamp(424242))


@pytest.mark.skipif(os.name != "posix", reason="kill(0) probe is POSIX only")
def test_real_probe_sees_a_finished_child() -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    pid = child.pid
    assert holders._pid_gone(pid)  # pyright: ignore[reportPrivateUsage]
    assert not holders._pid_gone(os.getpid())  # pyright: ignore[reportPrivateUsage]
