"""Self-tests of the fake vendor SDKs and the fake apptainer CLI (P21-15)."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.backends.sdk_call import status_of
from tests.fakes.backend import fake_daytona, fake_modal, fake_vercel
from tests.fakes.backend.fake_apptainer import FakeApptainer
from tests.fakes.backend.sdk_kit import Cloud, public_names
from tests.fakes.clock import FakeClock

REPO = Path(__file__).resolve().parents[3]
SURFACES = json.loads((REPO / "tests/fakes/backend/sdk_surfaces.json").read_text("utf-8"))
START = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="the fakes run bash")


@pytest.fixture
def cloud(tmp_path: Path) -> Any:
    own = Cloud(tmp_path / "cloud", clock=FakeClock(START))
    yield own
    own.release()


# -- surfaces -------------------------------------------------------------------------------------


@pytest.mark.parametrize("module", [fake_modal, fake_daytona, fake_vercel])
def test_fake_names_exist_in_the_pinned_sdk(module: Any) -> None:
    for target, fake_cls in module.SURFACE.items():
        vendor = target.split("#", 1)[0]
        assert vendor in SURFACES["classes"], f"{vendor} missing from sdk_surfaces.json"
        allowed = set(SURFACES["classes"][vendor])
        extra = set(public_names(fake_cls)) - allowed
        assert not extra, f"{fake_cls.__name__} has names the SDK lacks: {sorted(extra)}"


def test_surface_versions_match_the_pins() -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text("utf-8"))["project"]
    extras = project["optional-dependencies"]
    pins = {}
    for extra in ("modal", "daytona", "vercel"):
        for requirement in extras[extra]:
            name, _, version = requirement.partition("==")
            pins[name] = version
    assert pins == SURFACES["versions"]


# -- the kit --------------------------------------------------------------------------------------


def test_latency_follows_the_fake_clock(cloud: Cloud) -> None:
    assert cloud.clock is not None
    cloud.clock.allow_threaded_advance()
    modal = fake_modal.FakeModal(cloud).module
    cloud.delay("App.lookup", 5.0)
    done = threading.Event()

    def call() -> None:
        modal.App.lookup("indus", create_if_missing=True)
        done.set()

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    assert cloud.clock.wait_for_sleepers(1)
    cloud.clock.advance(4.9)
    assert not done.wait(0.1)
    cloud.clock.advance(0.1)
    assert done.wait(5)


def test_hang_until_released(cloud: Cloud) -> None:
    daytona = fake_daytona.FakeDaytona(cloud).module
    cloud.hang("Daytona.get")
    client = daytona.Daytona()
    outcome: list[BaseException] = []

    def call() -> None:
        try:
            client.get("anything")
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    worker.join(0.2)
    assert worker.is_alive()
    cloud.release()
    worker.join(5)
    assert outcome and isinstance(outcome[0], TimeoutError)


def test_status_scripted_errors_are_vendor_shaped(cloud: Cloud) -> None:
    modal = fake_modal.FakeModal(cloud).module
    cloud.fail("App.lookup", 503, times=2)
    for _ in range(2):
        with pytest.raises(modal.exception.ServiceError) as caught:
            modal.App.lookup("indus", create_if_missing=True)
        assert caught.value.status is fake_modal.FakeGrpcStatus.UNAVAILABLE
        assert status_of(caught.value) == 503
    assert modal.App.lookup("indus", create_if_missing=True).name == "indus"

    daytona = fake_daytona.FakeDaytona(cloud).module
    cloud.fail("Daytona.get", 429)
    with pytest.raises(daytona.DaytonaRateLimitError) as limited:
        daytona.Daytona().get("x")
    assert limited.value.status_code == 429

    vercel = fake_vercel.FakeVercel(cloud)
    client = vercel.sync_module.SyncSandboxClient.create(
        options=vercel.sync_module.SandboxServiceOptions(credentials_factory=vercel.credentials())
    )
    cloud.fail("SandboxClient.create_sandbox", 502)
    with pytest.raises(vercel.sync_module.SandboxApiError) as bad:
        client.create_sandbox(name="x")
    assert bad.value.status_code == 502 and status_of(bad.value) == 502
    assert cloud.methods().count("SandboxClient.create_sandbox") == 1


def test_toy_fs_round_trip_and_tar(cloud: Cloud, tmp_path: Path) -> None:
    fs = cloud.fs
    fs.write("/vercel/sandbox/a/b.txt", b"hello")
    assert fs.read("/vercel/sandbox/a/b.txt") == b"hello"
    assert fs.host("/vercel/sandbox/a/b.txt").is_relative_to(cloud.root / "_vfs")
    inside = cloud.root / "work" / "c.txt"
    fs.write(str(inside), b"direct")
    assert inside.read_bytes() == b"direct"  # a path under the root is used as is
    archive = fs.export_tar("/vercel/sandbox")
    fs.import_tar(archive, "/copy")
    assert fs.read("/copy/a/b.txt") == b"hello"
    tree = fs.capture()
    fs.wipe()
    assert not fs.exists("/copy/a/b.txt")
    fs.restore(tree)
    assert fs.read("/copy/a/b.txt") == b"hello" and inside.read_bytes() == b"direct"


# -- per vendor ---------------------------------------------------------------------------------


@needs_bash
def test_fake_modal_lifecycle(cloud: Cloud) -> None:
    fake = fake_modal.FakeModal(cloud)
    modal = fake.module
    app = modal.App.lookup("indus-agent", create_if_missing=True)
    sandbox = modal.Sandbox.create(
        "sleep", "infinity", app=app, image=modal.Image.from_registry("debian:12"), timeout=3600
    )
    work = cloud.root / "work"
    proc = sandbox.exec("bash", "-c", f"mkdir -p {work} && cat > {work}/f && echo done", text=True)
    payload = b"x" * (64 * 1024 + 5)
    proc.stdin.write(payload[: 64 * 1024])
    proc.stdin.drain()
    proc.stdin.write(payload[64 * 1024 :])
    proc.stdin.write_eof()
    proc.stdin.drain()
    assert proc.stdout.read() == "done\n" and proc.wait() == 0
    assert (work / "f").read_bytes() == payload
    image = sandbox.snapshot_filesystem(ttl=None)
    sandbox.terminate()
    with pytest.raises(modal.exception.SandboxTerminatedError):
        sandbox.exec("true")
    restored = modal.Sandbox.create(app=app, image=modal.Image.from_id(image.object_id))
    assert (work / "f").read_bytes() == payload
    raw = restored.exec("bash", "-c", "printf '\\377'", text=False)
    assert raw.stdout.read() == b"\xff"
    with pytest.raises(modal.exception.NotFoundError):
        modal.Sandbox.create(app=app, image=modal.Image.from_id("im-gone"))
    assert "Sandbox.snapshot_filesystem" in cloud.methods()


@needs_bash
def test_fake_daytona_lifecycle(cloud: Cloud) -> None:
    fake = fake_daytona.FakeDaytona(cloud, api_key="k" + "1")
    daytona = fake.module
    with pytest.raises(daytona.DaytonaAuthenticationError):
        daytona.Daytona(daytona.DaytonaConfig(api_key="wrong"))
    client = daytona.Daytona(daytona.DaytonaConfig(api_key="k" + "1"))
    params = daytona.CreateSandboxFromImageParams(
        image="debian:12",
        name="indus-t1",
        auto_stop_interval=0,
        resources=daytona.Resources(cpu=1, memory=5, disk=10),
        env_vars={"A": "1"},
    )
    sandbox = client.create(params)
    assert sandbox.state == daytona.SandboxState.STARTED and sandbox.auto_stop_interval == 0
    home = sandbox.get_user_home_dir()
    response = sandbox.process.exec("bash -c 'echo $A; echo err >&2; exit 4'", cwd=home)
    assert response.exit_code == 4 and response.result == "1\nerr\n"
    sandbox.fs.upload_file(b"abc", f"{home}/x.txt")
    assert sandbox.fs.get_file_info(f"{home}/x.txt").size == 3
    assert sandbox.fs.download_file(f"{home}/x.txt") == b"abc"
    sandbox.stop()
    with pytest.raises(daytona.DaytonaConflictError):
        sandbox.process.exec("true")
    again = client.get("indus-t1")
    assert again is sandbox and again.state == daytona.SandboxState.STOPPED
    again.start()
    assert again.fs.download_file(f"{home}/x.txt") == b"abc"  # stop keeps files
    client.delete(again)
    with pytest.raises(daytona.DaytonaNotFoundError):
        client.get("indus-t1")


@needs_bash
def test_fake_vercel_lifecycle(cloud: Cloud) -> None:
    fake = fake_vercel.FakeVercel(cloud, token="t" + "1")
    sync = fake.sync_module
    bad = sync.SyncSandboxClient.create(
        options=sync.SandboxServiceOptions(credentials_factory=fake.credentials(token="nope"))
    )
    with pytest.raises(sync.SandboxApiError):
        bad.create_sandbox(name="x")
    client = sync.SyncSandboxClient.create(
        options=sync.SandboxServiceOptions(credentials_factory=fake.credentials(token="t" + "1"))
    )
    sandbox = client.create_sandbox(name="indus-t1", image="node24", persistent=True)
    assert sandbox.status == sync.SandboxStatus.RUNNING and sandbox.cwd == "/vercel/sandbox"
    sandbox.fs.write_bytes("/vercel/sandbox/n.txt", b"note", mode=0o600)
    assert sandbox.fs.read_bytes("n.txt") == b"note"
    done = sandbox.run_process("bash", ["-c", 'printf %s "$X"; exit 2'], env={"X": "v"})
    assert done.returncode == 2 and done.stdout == "v"
    snapshot = sandbox.snapshot()
    assert sandbox.status == sync.SandboxStatus.STOPPED
    sandbox.destroy()
    fresh = client.create_sandbox(name="indus-t2", source=sync.SnapshotSource(snapshot.id))
    assert fresh.fs.read_bytes("/vercel/sandbox/n.txt") == b"note"
    fresh.stop()
    assert fresh.fs.exists("/vercel/sandbox/n.txt") and fake.resumes == 1
    with pytest.raises(sync.SandboxPathNotFoundError):
        fresh.fs.read_bytes("/vercel/sandbox/missing")


# -- the fake apptainer -------------------------------------------------------------------------


def _run(
    shim: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - the fake CLI
        [str(shim), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
    )


@needs_bash
def test_fake_apptainer_cli(tmp_path: Path) -> None:
    fake = FakeApptainer(tmp_path / "apptainer")
    shim = fake.install()
    assert _run(shim, "version").stdout.strip() == "apptainer version 1.3.4"
    sif = tmp_path / "cache" / "img.sif"
    assert _run(shim, "build", "--force", str(sif), "docker://debian:12").returncode == 0
    assert sif.exists() and fake.builds()[0]["source"] == "docker://debian:12"
    start = _run(
        shim,
        "instance",
        "start",
        "--containall",
        "--no-home",
        "--writable-tmpfs",
        "--bind",
        "/etc/hostname:/root/.indus/x:ro",
        str(sif),
        "indus_abc",
    )
    assert start.returncode == 0, start.stderr
    box = fake.instances()["indus_abc"]
    assert box["writable_tmpfs"] and box["binds"] == ["/etc/hostname:/root/.indus/x:ro"]
    ran = _run(
        shim,
        "exec",
        "instance://indus_abc",
        "bash",
        "-c",
        'printf %s "$SECRET_NAME"',
        env={"APPTAINERENV_SECRET_NAME": "value-1", "UNRELATED": "no"},
    )
    assert ran.returncode == 0 and ran.stdout == "value-1"
    assert fake.calls()[-1]["env_names"] == ["APPTAINERENV_SECRET_NAME"]
    assert "indus_abc" in _run(shim, "instance", "list").stdout
    assert _run(shim, "instance", "stop", "indus_abc").returncode == 0
    gone = _run(shim, "exec", "instance://indus_abc", "true")
    assert gone.returncode == 255 and "no instance found" in gone.stderr
    fake.set_version_fails()
    assert _run(shim, "version").returncode == 255
    other = fake.install("singularity")
    fake.set_version_fails(False)
    assert _run(other, "version").stdout.startswith("singularity-ce version 4")
    assert fake.subcommands()[:3] == ["version", "build", "instance start"]
