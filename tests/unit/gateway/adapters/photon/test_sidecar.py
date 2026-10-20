"""Sidecar ownership: spawn, adopt, refuse, stop, dependency self-heal (P16-25 ``sidecar``)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import sys
import time
from collections.abc import Callable, Coroutine, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.gateway.adapters.photon.sidecar import (
    DEFAULT_PORT,
    NPM_TIMEOUT_S,
    DependencyHealer,
    PhotonSidecar,
    SidecarError,
    exit_reason,
    http_probe,
    needs_install,
    provider_package,
    run_npm,
    state_dir,
)
from tests.fakes.clock import FakeClock
from tests.fakes.platform.photon_wire import FakeSidecar, FakeSpawner

# Built at run time: never a credential-shaped literal in the tree.
SECRET = "-".join(("project", "secret", "for", "tests"))


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))


def run[T](main: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(main())


class Rig:
    def __init__(self, root: Path, profile: str = "work", *, sidecar: FakeSidecar | None = None):
        self.clock = _clock()
        self.sidecar = sidecar or FakeSidecar()
        self.sidecar.platform.clock = self.clock
        self.sidecar.alive = False
        self.spawner = FakeSpawner(self.sidecar)
        self.envs: list[dict[str, str]] = []
        self.root = root
        self.home = root / "profiles" / profile
        self.home.mkdir(parents=True, exist_ok=True)
        script = root / "sidecar" / "main.ts"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("// sidecar\n")
        self.script = script
        self.provider = root / "provider" / "dist" / "index.mjs"
        self.provider.parent.mkdir(parents=True, exist_ok=True)
        self.provider.write_text("export const createSpectrumLink = null;\n")
        self.owner = self.make_owner(profile)

    def env_for(self, extra: dict[str, str]) -> dict[str, str]:
        env = {"PATH": "/usr/bin", **extra}
        self.envs.append(env)
        return env

    def make_owner(self, profile: str, **kw: Any) -> PhotonSidecar:
        kw.setdefault("node", "/usr/bin/node")
        kw.setdefault("script", self.script)
        kw.setdefault("provider", str(self.provider))
        kw.setdefault("project_id", "project-1")
        kw.setdefault("project_secret", SECRET)
        kw.setdefault("spawner", self.spawner)
        return PhotonSidecar(
            home=self.home,
            profile_id=profile,
            clock=self.clock,
            probe=http_probe(self.sidecar.transport),
            env_for=self.env_for,
            start_time=self.spawner.start_time,
            alive_check=self.spawner.alive,
            kill=self.spawner.kill,
            is_free=lambda port: not self.sidecar.alive,
            parent_pid=lambda: 4242,
            **kw,
        )


def test_a_fresh_sidecar_gets_its_token_and_credentials_through_its_env_only(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)

    async def main() -> None:
        endpoint = await rig.owner.ensure()
        assert endpoint.url == f"http://127.0.0.1:{DEFAULT_PORT}" == "http://127.0.0.1:8789"
        assert endpoint.token == rig.sidecar.token
        assert "token" not in repr(endpoint)
        assert SECRET not in repr(rig.owner)
        env = rig.envs[-1]
        assert env["INDUS_PHOTON_TOKEN"] == endpoint.token
        assert env["INDUS_PHOTON_HOST"] == "127.0.0.1"
        assert env["INDUS_PHOTON_PORT"] == "8789"
        assert env["INDUS_PHOTON_PROFILE"] == "work"
        assert env["INDUS_PHOTON_PARENT_PID"] == "4242"
        assert env["INDUS_PHOTON_PROVIDER"] == str(rig.provider)
        assert env["PHOTON_PROJECT_ID"] == "project-1"
        assert env["PHOTON_PROJECT_SECRET"] == SECRET
        assert rig.spawner.argvs == [["/usr/bin/node", str(rig.script)]]
        argv = " ".join(rig.spawner.argvs[0])
        assert endpoint.token not in argv and SECRET not in argv
        state = state_dir(rig.home, "work")
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
        for name in ("sidecar.token", "sidecar.pid", "sidecar.log"):
            assert stat.S_IMODE((state / name).stat().st_mode) == 0o600
        child = rig.spawner.children[0]
        assert json.loads((state / "sidecar.pid").read_text()) == {
            "pid": child.pid,
            "start": child.pid * 10,
            "port": 8789,
            "profile": "work",
        }
        await rig.owner.stop()

    run(main)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"provider": ""}, "photon_provider_missing"),
        ({"project_id": ""}, "photon_credentials_missing"),
        ({"project_secret": ""}, "photon_credentials_missing"),
        ({"node": None}, "photon_node_missing"),
        ({"script": Path("/nonexistent/main.ts")}, "photon_sidecar_missing"),
    ],
)
def test_missing_settings_fail_for_good_before_anything_runs(
    tmp_path: Path, change: dict[str, Any], code: str
) -> None:
    rig = Rig(tmp_path)
    owner = rig.make_owner("work", **change)

    async def main() -> None:
        with pytest.raises(SidecarError) as raised:
            await owner.ensure()
        assert raised.value.code == code
        assert not raised.value.retryable
        assert rig.spawner.argvs == []
        assert SECRET not in str(raised.value)

    run(main)


def test_a_port_setting_that_is_not_a_port_is_refused(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    with pytest.raises(SidecarError) as raised:
        rig.make_owner("work", port=70000)
    assert raised.value.code == "config"


def test_a_second_profile_cannot_adopt_or_stop_the_first_profiles_sidecar(tmp_path: Path) -> None:
    first = Rig(tmp_path, "work")
    second = first.make_owner("other")

    async def main() -> None:
        await first.owner.ensure()
        child = first.spawner.children[0]
        with pytest.raises(SidecarError) as raised:
            await second.ensure()
        assert raised.value.code == "photon_sidecar_port_taken"
        assert not raised.value.retryable
        assert child.signals == [] and child.returncode is None
        with pytest.raises(SidecarError) as attached:
            await second.attach()
        assert attached.value.code == "photon_sidecar_not_running"
        await second.stop()
        assert child.signals == []
        await first.owner.stop()

    run(main)


def test_a_restarted_adapter_adopts_its_own_running_sidecar(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def main() -> None:
        first = await rig.owner.ensure()
        again = rig.make_owner("work")
        adopted = await again.ensure()
        assert adopted.token == first.token
        assert len(rig.spawner.children) == 1
        assert again.pid == rig.spawner.children[0].pid
        attached = await rig.make_owner("work").attach()
        assert attached.token == first.token
        await again.stop()
        assert rig.spawner.children[0].signals == [signal.SIGTERM]
        assert not again.pidfile.exists() and not again.token_file.exists()

    run(main)


def test_a_recycled_pid_is_not_ours_and_is_never_signalled(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def main() -> None:
        await rig.owner.ensure()
        old = rig.spawner.children[0]
        # The process died and its pid now belongs to something else (another start time).
        old.returncode = 0
        rig.sidecar.alive = False
        again = rig.make_owner("work")
        await again.ensure()
        assert len(rig.spawner.children) == 2
        assert old.signals == []
        await again.stop()

    run(main)


@pytest.mark.parametrize(
    ("line", "exit_code", "error", "retryable"),
    [
        ('{"event":"error","error":"no_provider"}', 2, "photon_provider_missing", False),
        ('{"event":"error","error":"credentials"}', 2, "photon_credentials_missing", False),
        ('{"event":"error","error":"provider"}', 2, "photon_provider_failed", False),
        ('{"event":"error","error":"bad_token"}', 2, "photon_sidecar_config", False),
        ("", 1, "photon_sidecar_exited", True),
    ],
)
def test_an_exit_at_start_is_read_from_the_sidecars_own_line(
    tmp_path: Path, line: str, exit_code: int, error: str, retryable: bool
) -> None:
    rig = Rig(tmp_path)
    rig.spawner.crash_at_start = 1
    rig.spawner.crash_code = exit_code
    rig.spawner.crash_line = line

    async def main() -> None:
        with pytest.raises(SidecarError) as raised:
            await rig.owner.ensure()
        assert (raised.value.code, raised.value.retryable) == (error, retryable)
        assert rig.owner.pid is None and not rig.owner.pidfile.exists()

    run(main)


def test_exit_reason_reads_the_last_error_line_only(tmp_path: Path) -> None:
    log = tmp_path / "sidecar.log"
    log.write_text('{"event":"listening","port":1}\nnot json\n{"event":"error","error":"listen"}\n')
    assert exit_reason(log) == "listen"
    assert exit_reason(tmp_path / "missing.log") == ""


def test_a_sidecar_that_never_answers_is_stopped_and_retried_later(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def silent(
        argv: Sequence[str], env: Mapping[str, str], log_path: Path
    ) -> Any:  # a child that starts but never serves
        child = await FakeSpawner(rig.sidecar)(argv, env, log_path)
        rig.spawner.children.append(child)
        rig.sidecar.alive = False
        return child

    owner = rig.make_owner("work", spawner=silent)

    async def main() -> None:
        task = asyncio.ensure_future(owner.ensure())
        for _ in range(100):
            await rig.clock.run_until_idle()
            if task.done():
                break
            rig.clock.advance(0.5)
        with pytest.raises(SidecarError) as raised:
            await task
        assert raised.value.code == "photon_sidecar_timeout" and raised.value.retryable
        assert rig.spawner.children[0].signals[:1] == [signal.SIGTERM]

    run(main)


# -- dependency self-heal ------------------------------------------------------------------------


def _package(folder: Path, *, lock: bool = True, installed: bool = False) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "package.json").write_text('{"name": "provider"}\n')
    if lock:
        (folder / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    if installed:
        (folder / "node_modules").mkdir()
        (folder / "node_modules" / ".package-lock.json").write_text("{}\n")
    return folder


def test_the_provider_package_is_the_nearest_folder_with_a_package_json(tmp_path: Path) -> None:
    folder = _package(tmp_path / "provider")
    deep = folder / "dist" / "esm" / "index.mjs"
    deep.parent.mkdir(parents=True)
    deep.write_text("")
    assert provider_package(str(deep)) == folder
    assert provider_package("some-package-name") is None
    assert provider_package("relative/index.mjs") is None
    assert provider_package("") is None


def test_needs_install_follows_the_lock_file(tmp_path: Path) -> None:
    assert not needs_install(_package(tmp_path / "nolock", lock=False))
    assert needs_install(_package(tmp_path / "fresh"))
    done = _package(tmp_path / "done", installed=True)
    assert not needs_install(done)
    later = time.time() + 60
    os.utime(done / "package-lock.json", (later, later))
    assert needs_install(done)


class Runner:
    def __init__(self, result: int | None, *, install: Path | None = None) -> None:
        self.result = result
        self.install = install
        self.calls: list[tuple[list[str], Path, dict[str, str], float]] = []

    async def __call__(
        self, argv: Sequence[str], folder: Path, env: Mapping[str, str], timeout: float
    ) -> int | None:
        self.calls.append((list(argv), folder, dict(env), timeout))
        if self.install is not None and self.result == 0:
            (self.install / "node_modules").mkdir(exist_ok=True)
            (self.install / "node_modules" / ".package-lock.json").write_text("{}\n")
        return self.result


def test_npm_ci_heals_a_missing_install_without_scripts_bounded_to_600_s(tmp_path: Path) -> None:
    folder = _package(tmp_path / "provider")
    runner = Runner(0, install=folder)
    healer = DependencyHealer(
        npm="/usr/bin/npm", env_for=lambda e: {"PATH": "/usr/bin", **e}, runner=runner
    )

    async def main() -> None:
        await healer.heal(folder)
        await healer.heal(folder)

    run(main)
    assert NPM_TIMEOUT_S == 600.0
    assert runner.calls == [
        (
            ["/usr/bin/npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            folder,
            {"PATH": "/usr/bin"},
            600.0,
        )
    ]
    assert healer.runs == 1


@pytest.mark.parametrize(
    ("result", "npm", "code", "retryable"),
    [
        (None, "/usr/bin/npm", "photon_deps_timeout", True),
        (1, "/usr/bin/npm", "photon_deps_failed", False),
        (0, "/usr/bin/npm", "photon_deps_failed", False),  # exit 0 but still not installed
        (0, None, "photon_npm_missing", False),
    ],
)
def test_npm_ci_failures(
    tmp_path: Path, result: int | None, npm: str | None, code: str, retryable: bool
) -> None:
    folder = _package(tmp_path / "provider")
    healer = DependencyHealer(npm=npm, env_for=dict, runner=Runner(result))

    async def main() -> None:
        with pytest.raises(SidecarError) as raised:
            await healer.heal(folder)
        assert (raised.value.code, raised.value.retryable) == (code, retryable)

    run(main)


def test_ensure_heals_the_provider_package_before_the_spawn(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    folder = _package(tmp_path / "provider")
    runner = Runner(1)
    owner = rig.make_owner(
        "work", healer=DependencyHealer(npm="/usr/bin/npm", env_for=dict, runner=runner)
    )

    async def main() -> None:
        with pytest.raises(SidecarError) as raised:
            await owner.ensure()
        assert raised.value.code == "photon_deps_failed"
        assert rig.spawner.argvs == []
        assert runner.calls[0][1] == folder

    run(main)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_run_npm_stops_a_command_at_its_bound(tmp_path: Path) -> None:
    async def main() -> None:
        started = time.monotonic()
        assert await run_npm(["sleep", "30"], tmp_path, {"PATH": os.environ["PATH"]}, 0.3) is None
        assert time.monotonic() - started < 10
        assert await run_npm(["true"], tmp_path, {"PATH": os.environ["PATH"]}, 10) == 0
        assert await run_npm(["false"], tmp_path, {"PATH": os.environ["PATH"]}, 10) == 1

    run(main)
