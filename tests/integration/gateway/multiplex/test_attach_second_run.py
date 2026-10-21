"""P24-01 across processes: a real host owner (lock, record, control socket) in a child process
and a second ``gateway run`` decision in this one."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.gateway.multiplex.host import HostLock, decide_second_run, read_record
from tests.unit.gateway.multiplex.world import make_profile

OWNER = r"""
import asyncio
import sys
from pathlib import Path

from indus.foundation.scope import EnvView, make_scope
from indus.gateway.control_socket import ControlServer, identity_payload
from indus.gateway.multiplex.host import MultiplexHost

root, state, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]


async def main() -> int:
    env = EnvView.from_process()
    host = MultiplexHost(make_scope(root, env=env), record_dir=state)
    if not host.claim():
        print("held", flush=True)
        return 3
    server = None
    if mode == "serve":
        def identity() -> dict:
            return {**identity_payload(root, env), **host.identify_fields()}

        server = ControlServer(root, identity=identity, env=env)
        for verb, (handler, seconds) in host.control_verbs().items():
            server.register(verb, handler, timeout=seconds)
        await server.start()
    print("ready", flush=True)
    await asyncio.get_running_loop().run_in_executor(None, sys.stdin.read)
    if server is not None:
        await server.stop()
    await host.aclose()
    return 0


sys.exit(asyncio.run(main()))
"""


class Owner:
    def __init__(self, root: Path, state: Path, mode: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", OWNER, str(root), str(state), mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline().strip()
        assert line == "ready", line

    @property
    def pid(self) -> int:
        return self.proc.pid

    def stop(self) -> int:
        if self.proc.stdin is not None and not self.proc.stdin.closed:
            self.proc.stdin.close()
        try:
            return self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait(timeout=20)

    def kill(self) -> None:
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(timeout=20)


@pytest.fixture
def root(isolated_home: Path) -> Path:
    (isolated_home / "config.yaml").touch()
    return isolated_home


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "st"


@pytest.fixture
def owners(root: Path, state: Path) -> Iterator[list[Owner]]:
    started: list[Owner] = []
    yield started
    for owner in started:
        if owner.proc.poll() is None:
            owner.stop()
        if owner.proc.stdout is not None:
            owner.proc.stdout.close()


def start_owner(owners: list[Owner], root: Path, state: Path, mode: str = "serve") -> Owner:
    owner = Owner(root, state, mode)
    owners.append(owner)
    return owner


def test_a_second_run_for_a_served_profile_attaches_after_identify(
    root: Path, state: Path, owners: list[Owner]
) -> None:
    owner = start_owner(owners, root, state)
    shell = decide_second_run("default", root=root, record_dir=state, env={})
    assert (shell.outcome, shell.exit_code, shell.owner_pid) == ("attach", 0, owner.pid)
    supervised = {"INDUS_GATEWAY_SUPERVISED": "1"}
    service = decide_second_run("default", root=root, record_dir=state, env=supervised)
    assert (service.outcome, service.exit_code) == ("attach", 75)

    make_profile(root, "b")
    verdict = decide_second_run("b", root=root, record_dir=state, env={})
    assert (verdict.outcome, verdict.exit_code) == ("attach", 0)
    record = read_record(state)
    assert record is not None and record.served_profiles == ("default", "b")

    refused = decide_second_run("nobody", root=root, record_dir=state, env=supervised)
    assert (refused.outcome, refused.exit_code, refused.transient) == ("refuse", 78, False)
    assert owner.stop() == 0
    assert read_record(state) is None  # a clean exit removes its record


def test_a_record_without_a_live_answer_refuses_with_75(
    root: Path, state: Path, owners: list[Owner]
) -> None:
    owner = start_owner(owners, root, state, mode="silent")
    verdict = decide_second_run("default", root=root, record_dir=state, env={}, wait=0.6)
    assert (verdict.outcome, verdict.exit_code, verdict.transient) == ("refuse", 75, True)
    assert verdict.owner_pid == owner.pid
    assert owner.stop() == 0


def test_a_killed_owner_leaves_a_stale_record_and_the_next_run_starts(
    root: Path, state: Path, owners: list[Owner]
) -> None:
    owner = start_owner(owners, root, state)
    owner.kill()
    record = read_record(state)
    assert record is not None and record.pid == owner.pid  # left behind by the crash
    assert HostLock(state).observed_free() is True  # the kernel dropped the lock
    verdict = decide_second_run("default", root=root, record_dir=state, env={})
    assert verdict.outcome == "start"
    assert os.path.exists(state)
