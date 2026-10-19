"""P17-03 across real processes: one writer per session (ACC-A63-1).

Child processes (``lease_worker.py``) take the durable turn lease of one session in a temp home
with the real clock and short timings (TTL 3 s, renewal every 0.5 s). A second process waits
while the first holds it and proceeds once it releases; a SIGKILLed holder is taken over — at
once when it is provably dead in our pid namespace, and only after its expiry when it cannot be
probed (another namespace).
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.foundation.clock import SystemClock
from indus.foundation.scope import EnvView, make_scope
from indus.service.turn_lease import HeldLease, LeaseSettings, TurnLeases
from indus.state import registry, turn_leases
from indus.state.registry import StoreHandle
from indus.state.turn_leases import ProcessProbe, default_probe

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX processes and signals")

WORKER = Path(__file__).with_name("lease_worker.py")
ROOT = Path(__file__).resolve().parents[3]
SETTINGS = LeaseSettings(ttl=3.0, refresh_every=0.5, write_patience=1.0, poll=0.05, wait=60.0)


@pytest.fixture
def home(isolated_home: Path) -> Iterator[Path]:
    path = isolated_home / "profiles" / "work"
    path.mkdir(parents=True, exist_ok=True)
    (path / "profile.yaml").touch()
    yield path
    registry.close_all()


def _open(home: Path) -> StoreHandle:
    env = EnvView.from_process().with_overlay({"INDUS_HOME": str(home)})
    return registry.open_unshared(make_scope(home, env=env, clock=SystemClock()))


def _spawn(mode: str, home: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(WORKER), mode, str(home), "s1"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(ROOT),
    )


def _line(child: subprocess.Popen[str]) -> str:
    assert child.stdout is not None
    return child.stdout.readline().strip()


def _finish(*children: subprocess.Popen[str]) -> None:
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=30)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()


def test_second_process_waits_and_proceeds_after_release(home: Path) -> None:
    _open(home).close()  # create the store once, before the race
    first = _spawn("hold", home)
    second: subprocess.Popen[str] | None = None
    try:
        word, gen, holder = _line(first).split(" ", 2)
        assert word == "held" and gen == "1"
        second = _spawn("wait", home)
        assert _line(second) == "waiting"
        threading.Event().wait(1.5)  # several renewals: the holder keeps it while it lives
        assert second.poll() is None
        assert first.stdin is not None
        first.stdin.write("\n")
        first.stdin.flush()
        assert _line(first) == "released"
        word2, gen2, holder2 = _line(second).split(" ", 2)
        assert word2 == "held" and gen2 == "2" and holder2 != holder
        assert _line(second) == "released"
        assert first.wait(timeout=30) == 0 and second.wait(timeout=30) == 0
    finally:
        _finish(first, *([second] if second is not None else []))


def _killed_holder(home: Path) -> tuple[subprocess.Popen[str], str]:
    _open(home).close()
    child = _spawn("hold", home)
    word, _, holder = _line(child).split(" ", 2)
    assert word == "held"
    child.send_signal(signal.SIGKILL)
    child.wait(timeout=30)
    return child, holder


def test_a_killed_holder_is_reclaimed(home: Path) -> None:
    child, holder = _killed_holder(home)
    try:
        store = _open(home)
        try:
            row = turn_leases.get(store, "s1")
            assert row is not None and row.holder_id == holder  # SIGKILL released nothing
            leases = TurnLeases(store, settings=SETTINGS)
            started = time.monotonic()
            held = asyncio.run(leases.acquire("s1"))
            assert isinstance(held, HeldLease) and held.generation == 2
            if default_probe().namespace() == row.holder_ns:
                assert time.monotonic() - started < SETTINGS.ttl  # provably dead: no wait
            asyncio.run(held.release())
        finally:
            store.close()
    finally:
        _finish(child)


def test_a_holder_from_another_namespace_is_reclaimed_only_after_expiry(home: Path) -> None:
    child, _ = _killed_holder(home)
    try:
        store = _open(home)
        try:
            real = default_probe()
            foreign = ProcessProbe(
                alive=real.alive,
                start_time=real.start_time,
                namespace=lambda: "another-container",
                host=real.host,
            )
            row = turn_leases.get(store, "s1")
            assert row is not None
            leases = TurnLeases(store, settings=SETTINGS, probe=foreign)
            held = asyncio.run(leases.acquire("s1"))
            assert isinstance(held, HeldLease)
            # never before the dead holder's committed expiry
            assert time.time() >= row.expires_at
            asyncio.run(held.release())
        finally:
            store.close()
    finally:
        _finish(child)
