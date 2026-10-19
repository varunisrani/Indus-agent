"""The Singularity / Apptainer backend over the fake CLI (P21-04, R02.6, R02.7).

The fake ``apptainer`` (``tests/fakes/backend/fake_apptainer.py``) records argv and instance state;
``exec`` runs real bash on this machine, so the shared shell flow runs for real. The instance's
private /tmp is pointed at the test's own dir.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from indus.backends import singularity
from indus.backends.contract import BackendUnavailable, EnvironmentConnectionError, ExecResult
from indus.backends.docker.mounts import (
    ReadonlyMount,
    register_readonly_source,
    unregister_readonly_source,
)
from indus.backends.registry import BackendStatus, CreateRequest, EnvironmentRegistry, EnvKey
from indus.backends.sandbox_records import SandboxRecords
from indus.backends.singularity import (
    NOT_FOUND_REASON,
    SINGULARITY_FLAGS,
    SingularityBuildError,
    SingularityCli,
    SingularityEnvironment,
    ensure_sif,
    open_singularity,
    settings_for,
    sif_path,
    singularity_entry,
    singularity_probe,
    warn_disk_usage,
)
from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope, make_scope
from tests.fakes.backend.fake_apptainer import FakeApptainer
from tests.fakes.clock import FakeClock

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="the fake CLI runs bash")

IMAGE = "docker://registry.test/indus-sandbox:test"
INSTANCE = re.compile(r"^indus_[0-9a-f]{12}$")


class Config:
    def __init__(self, values: Mapping[str, Any]) -> None:
        self.values = dict(values)

    def get(self, path: str, /) -> object:
        return self.values.get(path)


class Secrets:
    def __init__(self, values: Mapping[str, str]) -> None:
        self.values = dict(values)

    def get(self, name: str, /) -> str | None:
        return self.values.get(name)

    def known_values(self) -> list[str]:
        return list(self.values.values())

    @property
    def generation(self) -> int:
        return 1


@pytest.fixture(autouse=True)
def private_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    sandbox_tmp = tmp_path / "instance-tmp"
    sandbox_tmp.mkdir()
    monkeypatch.setattr(singularity, "SANDBOX_TEMP", str(sandbox_tmp))
    singularity.reset_for_tests()
    yield
    singularity.reset_for_tests()


@pytest.fixture
def fake(tmp_path: Path) -> FakeApptainer:
    cli = FakeApptainer(tmp_path / "fake-apptainer")
    cli.install()
    return cli


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    (path / "sub").mkdir(parents=True)
    return path


def make(
    tmp_path: Path,
    fake: FakeApptainer | None,
    clock: FakeClock,
    *,
    secrets: Mapping[str, str] | None = None,
    profile: str = "work",
    **config: Any,
) -> Scope:
    home = tmp_path / "root" / "profiles" / profile
    home.mkdir(parents=True, exist_ok=True)
    path = f"{fake.bin_dir}:/usr/bin:/bin" if fake is not None else "/nonexistent"
    values: dict[str, Any] = {
        "terminal.backend": "singularity",
        "terminal.singularity_image": IMAGE,
        "terminal.timeout": 30,
        "terminal.container_persistent": False,
    }
    values.update({k.replace("__", "."): v for k, v in config.items()})
    scope = make_scope(home, env={"PATH": path, "HOME": str(tmp_path)}, clock=clock)
    scope = scope.with_config(Config(values))
    return scope.with_secrets(Secrets(secrets)) if secrets is not None else scope


def request(scope: Scope, cwd: Path | None = None, *, ident: str = "sess-1") -> CreateRequest:
    key = EnvKey(str(scope.home), "session", ident, "singularity")
    return CreateRequest(scope, key, None, ident, {"cwd": str(cwd)} if cwd else {})


async def _open(scope: Scope, cwd: Path | None = None, **kwargs: Any) -> SingularityEnvironment:
    return await open_singularity(request(scope, cwd, **kwargs), cgroups=True)


def _starts(fake: FakeApptainer) -> list[list[str]]:
    return [argv for argv in fake.commands() if argv[:2] == ["instance", "start"]]


def test_start_flags_tmpfs_binds_and_a_command(
    tmp_path: Path, fake: FakeApptainer, fake_clock: FakeClock, work: Path
) -> None:
    scope = make(tmp_path, fake, fake_clock)
    (scope.home / "skills").mkdir()
    extra = tmp_path / "ro-source"
    extra.mkdir()

    def source(_: Scope) -> list[ReadonlyMount]:
        return [ReadonlyMount(str(extra), "/root/.indus/extra", "dir")]

    register_readonly_source(source)

    async def go() -> tuple[SingularityEnvironment, str, str | None]:
        env = await _open(scope, work)
        result = await env.execute("printf %s hello")
        await env.cleanup()
        return env, result.output, result.cwd_observed

    try:
        env, output, cwd = asyncio.run(go())
    finally:
        unregister_readonly_source(source)
    assert output == "hello" and cwd == str(work)
    (start,) = _starts(fake)
    sif = str(sif_path(scope.home / "sandboxes" / "singularity", IMAGE))
    assert start[2:5] == ["--containall", "--no-home", "--writable-tmpfs"]
    assert "--overlay" not in start
    assert start[-2] == sif and INSTANCE.match(start[-1])
    binds = [start[i + 1] for i, arg in enumerate(start) if arg == "--bind"]
    assert binds == [
        f"{scope.home / 'skills'}:/root/.indus/skills:ro",
        f"{extra}:/root/.indus/extra:ro",
    ]
    assert start[start.index("--cpus") + 1] == "1"
    assert start[start.index("--memory") + 1] == "5120M"
    assert env.flags == SINGULARITY_FLAGS and env.flags.isolated  # R02.6: no host path mounted
    assert fake.subcommands()[-1] == "instance stop" and fake.instances() == {}


def test_no_limits_without_cgroups(
    tmp_path: Path, fake: FakeApptainer, fake_clock: FakeClock
) -> None:
    scope = make(tmp_path, fake, fake_clock)

    async def go() -> None:
        env = await open_singularity(request(scope), cgroups=False)
        await env.cleanup()

    asyncio.run(go())
    (start,) = _starts(fake)
    assert "--cpus" not in start and "--memory" not in start


def test_cgroup_refusal_retried_without_limits(
    tmp_path: Path, fake: FakeApptainer, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    scope = make(tmp_path, fake, fake_clock)
    fake.start_fails("FATAL:   container creation failed: cgroups v2 manager unavailable\n")

    async def go() -> None:
        env = await _open(scope)
        await env.cleanup()

    with caplog.at_level(logging.WARNING, logger="indus.backends.singularity"):
        asyncio.run(go())
    first, second = _starts(fake)
    assert "--cpus" in first and "--cpus" not in second and "--memory" not in second
    assert any("without them" in r.getMessage() for r in caplog.records)


def test_start_failure_is_an_error(
    tmp_path: Path, fake: FakeApptainer, fake_clock: FakeClock
) -> None:
    scope = make(tmp_path, fake, fake_clock)
    fake.start_fails("FATAL:   image is corrupt\n")
    with pytest.raises(singularity.BackendError, match="image is corrupt"):
        asyncio.run(_open(scope))


def test_persistent_overlay_recorded_and_reused(
    tmp_path: Path, fake: FakeApptainer, fake_clock: FakeClock
) -> None:
    scope = make(tmp_path, fake, fake_clock, terminal__container_persistent=True)
    records = SandboxRecords.for_scope(scope, "singularity")

    async def cycle() -> SingularityEnvironment:
        env = await _open(scope)
        await env.execute("true")
        await env.cleanup()
        return env

    first = asyncio.run(cycle())
    assert first.overlay is not None
    overlay = scope.home / "sandboxes" / "singularity" / "overlays" / "sess-1"
    assert first.overlay == str(overlay) and overlay.is_dir()
    record = records.get("sess-1")
    assert record is not None and record.overlay_path == str(overlay)
    second = asyncio.run(cycle())
    assert second.overlay == first.overlay
    starts = _starts(fake)
    assert len(starts) == 2
    for start in starts:
        assert start[start.index("--overlay") + 1] == str(overlay)
        assert "--writable-tmpfs" not in start
    cleanups = [argv for argv in fake.commands() if argv[:1] == ["exec"] and argv[2] == "sh"]
    assert len(cleanups) == 2  # the snapshot is taken out of the overlay's /tmp before stopping


def test_singularity_fallback(tmp_path: Path, fake_clock: FakeClock) -> None:
    only = FakeApptainer(tmp_path / "fake-singularity")
    only.install("singularity")
    scope = make(
        tmp_path,
        only,
        fake_clock,
        terminal__env_passthrough=["PASS_ME"],
        secrets={"PASS_ME": "through"},
    )

    async def go() -> str:
        env = await _open(scope)
        try:
            assert env.cli.prog == "singularity" and env.cli.env_prefix == "SINGULARITYENV_"
            return (await env.execute('printf %s "$PASS_ME"')).output
        finally:
            await env.cleanup()

    assert asyncio.run(go()) == "through"
    assert only.calls()[0]["prog"] == "singularity"
    assert "SINGULARITYENV_PASS_ME" in only.calls()[-2]["env_names"]


def test_missing_cli(tmp_path: Path, fake_clock: FakeClock) -> None:
    scope = make(tmp_path, None, fake_clock)
    assert singularity_probe(scope) == NOT_FOUND_REASON
    with pytest.raises(BackendUnavailable, match=NOT_FOUND_REASON):
        asyncio.run(_open(scope))

    async def through_registry() -> object:
        registry = EnvironmentRegistry([singularity_entry()])
        try:
            return await registry.run("true", scope=scope, session_id="s")
        finally:
            await registry.shutdown(fence=False)

    status = asyncio.run(through_registry())
    assert isinstance(status, BackendStatus) and status.status == "disabled"
    assert NOT_FOUND_REASON in status.error


def test_version_failure_is_unavailable(
    tmp_path: Path, fake: FakeApptainer, fake_clock: FakeClock
) -> None:
    fake.set_version_fails()
    with pytest.raises(BackendUnavailable, match="version` failed"):
        asyncio.run(_open(make(tmp_path, fake, fake_clock)))


def test_sif_built_once_for_two_environments(
    tmp_path: Path, fake: FakeApptainer, fake_clock: FakeClock
) -> None:
    scope = make(tmp_path, fake, fake_clock)

    async def go() -> None:
        first = await _open(scope, ident="a")
        second = await _open(scope, ident="b")
        assert first.sif == second.sif
        await first.cleanup()
        await second.cleanup()

    asyncio.run(go())
    builds = fake.builds()
    assert len(builds) == 1 and builds[0]["source"] == IMAGE and "--force" in builds[0]["options"]
    cached = sif_path(scope.home / "sandboxes" / "singularity", IMAGE)
    assert cached.is_file() and len(cached.stem) == 16
    assert not list(cached.parent.glob("*.partial.sif"))


def test_local_sif_is_used_as_is(
    tmp_path: Path, fake: FakeApptainer, fake_clock: FakeClock
) -> None:
    local = tmp_path / "mine.sif"
    local.write_text("SIF", encoding="utf-8")
    scope = make(tmp_path, fake, fake_clock, terminal__singularity_image=str(local))

    async def go() -> None:
        env = await _open(scope)
        await env.cleanup()

    asyncio.run(go())
    assert fake.builds() == [] and _starts(fake)[0][-2] == str(local)


def _cli(scope: Scope, fake: FakeApptainer) -> SingularityCli:
    return SingularityCli(
        str(fake.bin_dir / "apptainer"), singularity.client_env(scope, Path("/x"))
    )


def test_build_failure_is_typed(tmp_path: Path, fake: FakeApptainer, fake_clock: FakeClock) -> None:
    scope = make(tmp_path, fake, fake_clock)
    fake.build_fails("FATAL: " + "e" * 3000 + " registry said no\n")
    scratch = tmp_path / "scratch"
    with pytest.raises(SingularityBuildError) as caught:
        asyncio.run(ensure_sif(_cli(scope, fake), IMAGE, scratch))
    message = str(caught.value)
    assert message.endswith("registry said no") and "failed" in message
    assert len(message.split(": ", 1)[1]) <= 500
    assert caught.value.code == "backend.singularity.build"
    assert not list((scratch / "sif-cache").glob("*.sif"))


def test_build_timeout_is_typed(tmp_path: Path, fake: FakeApptainer, fake_clock: FakeClock) -> None:
    scope = make(tmp_path, fake, fake_clock)
    fake.set_build_delay(20)
    scratch = tmp_path / "scratch"
    with pytest.raises(SingularityBuildError) as caught:
        asyncio.run(ensure_sif(_cli(scope, fake), IMAGE, scratch, timeout=1.5))
    message = str(caught.value)
    assert "timed out after 1.5s" in message
    detail = message.split(": ", 1)[1]
    assert 0 < len(detail) <= 500 and set(detail) <= {"x"}  # the tail of what it printed
    assert not list((scratch / "sif-cache").glob("*.sif"))


def test_vanished_instance_is_a_connection_error(
    tmp_path: Path, fake: FakeApptainer, fake_clock: FakeClock
) -> None:
    scope = make(tmp_path, fake, fake_clock)

    async def go() -> None:
        env = await _open(scope)
        assert env.instance is not None
        fake.stop_instance(env.instance)
        with pytest.raises(EnvironmentConnectionError, match="instance is gone"):
            await env.execute("true")
        await env.cleanup()

    asyncio.run(go())

    async def through_registry() -> tuple[ExecResult | BackendStatus, ExecResult | BackendStatus]:
        registry = EnvironmentRegistry([singularity_entry()])
        try:
            _, env = await registry.acquire(scope, session_id="s")
            assert isinstance(env, SingularityEnvironment) and env.instance is not None
            fake.stop_instance(env.instance)
            degraded = await registry.run("true", scope=scope, session_id="s")
            recovered = await registry.run("printf %s ok", scope=scope, session_id="s")
            return degraded, recovered
        finally:
            await registry.shutdown(fence=False)

    degraded, recovered = asyncio.run(through_registry())
    assert isinstance(degraded, BackendStatus) and degraded.status == "degraded"
    assert isinstance(recovered, ExecResult) and recovered.output == "ok"


def test_passthrough_values_travel_in_the_client_env(
    tmp_path: Path, fake: FakeApptainer, fake_clock: FakeClock
) -> None:
    value = "pt-" + "value-" + "77"
    scope = make(
        tmp_path,
        fake,
        fake_clock,
        terminal__env_passthrough=["PASS_ME", "NOT_SET_NOW"],
        secrets={"PASS_ME": value},
    )

    async def go() -> str:
        env = await _open(scope)
        try:
            return (await env.execute('printf %s "$PASS_ME:${NOT_SET_NOW-unset}"')).output
        finally:
            await env.cleanup()

    assert asyncio.run(go()) == f"{value}:unset"
    for call in fake.calls():
        assert all(value not in arg for arg in call["argv"])  # R02.7
    execs = [c for c in fake.calls() if c["argv"][:1] == ["exec"] and "bash" in c["argv"]]
    assert execs and all(c["env_names"] == ["APPTAINERENV_PASS_ME"] for c in execs)
    assert all("unset NOT_SET_NOW" in c["argv"][-1] for c in execs)


def test_disk_warning_once(
    tmp_path: Path, fake: FakeApptainer, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    scope = make(tmp_path, fake, fake_clock, terminal__disk_warning_gb=1e-9)

    async def go() -> None:
        for ident in ("a", "b"):
            env = await _open(scope, ident=ident)
            await env.cleanup()

    with caplog.at_level(logging.WARNING, logger="indus.backends.singularity"):
        asyncio.run(go())
    warnings = [r for r in caplog.records if "scratch dir" in r.getMessage()]
    assert len(warnings) == 1
    assert warn_disk_usage(tmp_path / "other", 0.0, usage=lambda _: 10**15) is False
    assert warn_disk_usage(tmp_path / "other", 1.0, usage=lambda _: 10) is False
    assert warn_disk_usage(tmp_path / "other", 1.0, usage=lambda _: 2 * 1024**3) is True
    assert warn_disk_usage(tmp_path / "other", 1.0, usage=lambda _: 2 * 1024**3) is False


def test_settings_defaults(tmp_path: Path, fake_clock: FakeClock) -> None:
    assert default_for("terminal.singularity_image") == ""
    assert default_for("terminal.scratch_dir") == ""
    assert default_for("terminal.disk_warning_gb") == 500
    home = tmp_path / "p"
    home.mkdir()
    scope = make_scope(home, env={"HOME": str(tmp_path)}, clock=fake_clock).with_config(Config({}))
    settings = settings_for(scope)
    assert settings.image == f"docker://{default_for('terminal.docker_image')}"
    assert settings.scratch_dir == home / "sandboxes" / "singularity"
    assert settings.disk_warning_gb == 500 and settings.persistent is True
    custom = scope.with_config(Config({"terminal.scratch_dir": "~/scratch"}))
    assert settings_for(custom).scratch_dir == tmp_path / "scratch"


def test_entry_row() -> None:
    entry = singularity_entry()
    assert entry.name == "singularity" and entry.flags == SINGULARITY_FLAGS
    assert entry.flags.stdin_mode == "pipe" and entry.flags.container_paths
    assert entry.probe is singularity_probe
