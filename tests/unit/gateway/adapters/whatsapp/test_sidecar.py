"""Bridge ownership: spawn, adopt, refuse, stop, ports (P16-03 ``sidecar``)."""

from __future__ import annotations

import asyncio
import json
import signal
import socket
import stat
import sys
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.gateway.adapters.whatsapp.adapter import http_probe
from indus.gateway.adapters.whatsapp.sidecar import (
    DEFAULT_PORT,
    SECONDARY_FIRST,
    BridgeError,
    PidRecord,
    SidecarOwner,
    bridge_port,
    port_free,
    spawn_process,
    state_dir,
)
from tests.fakes.clock import FakeClock
from tests.fakes.platform.whatsapp_wire import FakeBridge, FakeSpawner


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))


class Rig:
    def __init__(self, root: Path, profile: str = "work", *, bridge: FakeBridge | None = None):
        self.clock = _clock()
        self.bridge = bridge or FakeBridge()
        self.bridge.platform.clock = self.clock
        self.bridge.alive = False
        self.spawner = FakeSpawner(self.bridge)
        self.busy: set[int] = set()
        self.envs: list[dict[str, str]] = []
        self.root = root
        self.home = root if profile == "default" else root / "profiles" / profile
        self.home.mkdir(parents=True, exist_ok=True)
        script = root / "bridge" / "main.ts"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("// bridge\n")
        self.script = script
        self.owner = self.make_owner(profile)

    def env_for(self, extra: dict[str, str]) -> dict[str, str]:
        env = {"PATH": "/usr/bin", **extra}
        self.envs.append(env)
        return env

    def make_owner(self, profile: str, **kw: Any) -> SidecarOwner:
        kw.setdefault("explicit_port", 3000)
        kw.setdefault("node", "/usr/bin/node")
        kw.setdefault("script", self.script)
        return SidecarOwner(
            home=self.home,
            root=self.root,
            profile_id=profile,
            clock=self.clock,
            probe=http_probe(self.bridge.transport),
            env_for=self.env_for,
            spawner=self.spawner,
            start_time=self.spawner.start_time,
            alive_check=self.spawner.alive,
            kill=self.spawner.kill,
            is_free=lambda port: port not in self.busy and not self.bridge.alive,
            parent_pid=lambda: 4242,
            **kw,
        )


def run[T](main: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(main())


def test_a_fresh_bridge_gets_a_private_token_through_its_env_only(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def main() -> None:
        endpoint = await rig.owner.ensure()
        assert endpoint.url == "http://127.0.0.1:3000"
        assert endpoint.token == rig.bridge.token
        assert "token" not in repr(endpoint)
        env = rig.envs[-1]
        assert env["INDUS_WA_TOKEN"] == endpoint.token
        assert env["INDUS_WA_HOST"] == "127.0.0.1"
        assert env["INDUS_WA_PROFILE"] == "work"
        assert env["INDUS_WA_PARENT_PID"] == "4242"
        assert env["INDUS_WA_AUTH_DIR"] == str(state_dir(rig.home, "work") / "auth")
        assert rig.spawner.argvs == [["/usr/bin/node", str(rig.script)]]
        assert all(endpoint.token not in arg for arg in rig.spawner.argvs[0])
        state = state_dir(rig.home, "work")
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
        assert stat.S_IMODE((state / "bridge.token").stat().st_mode) == 0o600
        assert stat.S_IMODE((state / "bridge.pid").stat().st_mode) == 0o600
        record = json.loads((state / "bridge.pid").read_text())
        child = rig.spawner.children[0]
        assert record == {
            "pid": child.pid,
            "start": child.pid * 10,
            "port": 3000,
            "profile": "work",
        }

    run(main)


def test_a_second_profile_cannot_adopt_or_stop_the_first_profiles_bridge(tmp_path: Path) -> None:
    first = Rig(tmp_path, "work")

    async def main() -> None:
        await first.owner.ensure()
        second_home = tmp_path / "profiles" / "play"
        second_home.mkdir(parents=True)
        second = SidecarOwner(
            home=second_home,
            root=tmp_path,
            profile_id="play",
            clock=first.clock,
            probe=http_probe(first.bridge.transport),
            node="/usr/bin/node",
            script=first.script,
            env_for=first.env_for,
            explicit_port=3000,
            spawner=first.spawner,
            start_time=first.spawner.start_time,
            alive_check=first.spawner.alive,
            kill=first.spawner.kill,
            is_free=lambda port: not first.bridge.alive,
        )
        with pytest.raises(BridgeError) as raised:
            await second.ensure()
        assert raised.value.code == "whatsapp_bridge_port_taken"
        assert not raised.value.retryable
        assert len(first.spawner.children) == 1
        assert first.spawner.children[0].signals == []
        assert first.bridge.profile == "work"
        await second.stop()
        assert first.spawner.children[0].signals == []
        with pytest.raises(BridgeError) as attached:
            await second.attach()
        assert attached.value.code == "whatsapp_bridge_not_running"

    run(main)


def test_a_port_answered_by_someone_elses_bridge_is_left_alone(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def foreign(argv: Any, env: Any, log_path: Path) -> Any:
        child = await FakeSpawner.__call__(rig.spawner, argv, env, log_path)
        rig.bridge.restart(token="another-" + "x" * 8, profile="play")
        return child

    rig.owner._spawner = foreign  # pyright: ignore[reportPrivateUsage]

    async def main() -> None:
        with pytest.raises(BridgeError) as raised:
            await rig.owner.ensure()
        assert raised.value.code == "whatsapp_bridge_port_taken"
        # Our own child was stopped; the foreign bridge itself was never signalled.
        assert rig.spawner.children[0].signals == [signal.SIGTERM]
        assert not (state_dir(rig.home, "work") / "bridge.pid").exists()

    run(main)


def test_a_restarted_adapter_adopts_its_own_running_bridge(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def main() -> None:
        first = await rig.owner.ensure()
        again = rig.make_owner("work")
        endpoint = await again.ensure()
        assert endpoint.token == first.token
        assert len(rig.spawner.children) == 1
        assert again.pid == rig.spawner.children[0].pid
        assert not again.exited()
        rig.spawner.crash()
        assert again.exited()

    run(main)


def test_a_recycled_pid_is_not_ours_and_an_unhealthy_bridge_of_ours_is_replaced(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)

    async def main() -> None:
        state = state_dir(rig.home, "work")
        state.mkdir(parents=True)
        # A pidfile naming a live process whose start time differs: never signalled.
        (state / "bridge.pid").write_text(
            PidRecord(4_000_001, 1, 3000, "work").to_json(), encoding="utf-8"
        )
        rig.spawner.next_pid = 4_000_001
        await rig.owner.ensure()
        assert rig.spawner.children[0].signals == []
        # Our bridge stops answering: a new owner replaces it.
        rig.bridge.token = "changed-" + "y" * 8
        again = rig.make_owner("work")
        await again.ensure()
        assert rig.spawner.children[0].signals == [signal.SIGTERM]
        assert len(rig.spawner.children) == 2

    run(main)


def test_without_a_provider_the_bridge_fails_closed_with_a_clear_error(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def main() -> None:
        # The bridge starts, finds no provider and exits 2 (bad settings, nothing bound).
        rig.spawner.crash_at_start = 1
        rig.spawner.crash_code = 2
        with pytest.raises(BridgeError) as missing:
            await rig.owner.ensure()
        assert missing.value.code == "whatsapp_provider_missing"
        assert not missing.value.retryable
        assert "platforms.whatsapp.provider" in str(missing.value)
        assert rig.envs[-1]["INDUS_WA_PROVIDER"] == ""
        assert not (state_dir(rig.home, "work") / "bridge.pid").exists()
        # With a provider named, an exit 2 is an ordinary start failure.
        named = rig.make_owner("work", provider="/opt/wa/provider.mjs")
        rig.spawner.crash_at_start = 1
        with pytest.raises(BridgeError) as other:
            await named.ensure()
        assert other.value.code == "whatsapp_bridge_exited"

    run(main)


def test_start_failures_are_retryable_and_tools_missing_is_not(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def main() -> None:
        rig.spawner.crash_at_start = 1
        with pytest.raises(BridgeError) as crashed:
            await rig.owner.ensure()
        assert crashed.value.code == "whatsapp_bridge_exited"
        assert crashed.value.retryable
        assert not (state_dir(rig.home, "work") / "bridge.token").exists()

        async def mute(argv: Any, env: Any, log_path: Path) -> Any:
            child = await FakeSpawner.__call__(rig.spawner, argv, env, log_path)
            rig.bridge.alive = False
            return child

        rig.owner._spawner = mute  # pyright: ignore[reportPrivateUsage]
        task = asyncio.ensure_future(rig.owner.ensure())
        for _ in range(40):
            await rig.clock.advance_and_settle(0.5)
        with pytest.raises(BridgeError) as slow:
            await task
        assert slow.value.code == "whatsapp_bridge_timeout"
        assert rig.spawner.children[-1].signals == [signal.SIGTERM]

        for owner, code in (
            (rig.make_owner("work", node=None), "whatsapp_node_missing"),
            (rig.make_owner("work", script=tmp_path / "nope.ts"), "whatsapp_bridge_missing"),
        ):
            with pytest.raises(BridgeError) as missing:
                await owner.ensure()
            assert missing.value.code == code
            assert not missing.value.retryable

    run(main)


def test_send_only_attaches_to_the_running_bridge_and_never_starts_one(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def main() -> None:
        with pytest.raises(BridgeError) as raised:
            await rig.owner.attach()
        assert raised.value.code == "whatsapp_bridge_not_running"
        assert rig.spawner.children == []
        started = await rig.owner.ensure()
        visitor = rig.make_owner("work")
        endpoint = await visitor.attach()
        assert endpoint.token == started.token
        await visitor.stop()
        assert rig.spawner.children[0].signals == []
        assert not visitor.exited()

    run(main)


def test_stop_ends_our_bridge_and_forgets_its_files(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def main() -> None:
        await rig.owner.ensure()
        await rig.owner.stop()
        assert rig.spawner.children[0].signals == [signal.SIGTERM]
        state = state_dir(rig.home, "work")
        assert not (state / "bridge.pid").exists()
        assert not (state / "bridge.token").exists()
        await rig.owner.stop()
        assert rig.spawner.children[0].signals == [signal.SIGTERM]

    run(main)


def test_ports_default_secondary_recorded_and_sibling_claims(tmp_path: Path) -> None:
    assert bridge_port(tmp_path, tmp_path, "default", None) == DEFAULT_PORT
    assert bridge_port(tmp_path, tmp_path, "work", 4100) == 4100
    with pytest.raises(BridgeError):
        bridge_port(tmp_path, tmp_path, "work", 70000)
    sibling = state_dir(tmp_path / "profiles" / "play", "play") / "bridge_port"
    sibling.parent.mkdir(parents=True)
    sibling.write_text(f"{SECONDARY_FIRST}\n")
    work_home = tmp_path / "profiles" / "work"
    busy = {SECONDARY_FIRST + 1}
    port = bridge_port(work_home, tmp_path, "work", None, is_free=lambda p: p not in busy)
    assert port == SECONDARY_FIRST + 2
    assert bridge_port(work_home, tmp_path, "work", None, is_free=lambda p: False) == port
    with pytest.raises(BridgeError) as none_free:
        bridge_port(tmp_path / "profiles" / "x", tmp_path, "x", None, is_free=lambda p: False)
    assert none_free.value.code == "whatsapp_no_port"


def test_port_free_sees_a_live_listener() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        assert not port_free(port)
    assert port_free(port)


def test_spawn_process_writes_an_owner_only_log(tmp_path: Path) -> None:
    log_path = tmp_path / "bridge.log"

    async def main() -> int | None:
        child = await spawn_process(
            [sys.executable, "-c", "print('started')"], {"PATH": "/usr/bin"}, log_path
        )
        assert isinstance(child, asyncio.subprocess.Process)
        return await child.wait()

    assert run(main) == 0
    assert log_path.read_text().strip() == "started"
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
