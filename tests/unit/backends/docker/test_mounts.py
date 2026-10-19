"""What the container sees of the host (P11-03)."""

from __future__ import annotations

import stat
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from indus.backends.docker.mounts import (
    ReadonlyMount,
    plan_mounts,
    readonly_from_sources,
    register_readonly_source,
    sandbox_dir_name,
    split_volume,
    unregister_readonly_source,
)
from indus.backends.docker.settings import DockerSettings
from indus.foundation.scope import Scope, make_scope

BASE = DockerSettings(image="img", persistent=False)


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


def _plan(tmp_path: Path, settings: DockerSettings, **kwargs: object):  # type: ignore[no-untyped-def]
    options: dict[str, object] = {"home": tmp_path / "home", "task_key": "task"}
    options.update(kwargs)
    return plan_mounts(settings, **options)  # type: ignore[arg-type]


def _pairs(args: tuple[str, ...], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


def test_cwd_bound_at_workspace_when_asked(tmp_path: Path, work: Path) -> None:
    plan = _plan(tmp_path, replace(BASE, mount_cwd_to_workspace=True), host_cwd=str(work))
    assert plan.cwd_target == "/workspace"
    assert _pairs(plan.volume_args, "-v") == [f"{work}:/workspace"]
    assert "/workspace:rw,exec,size=10g" not in plan.writable_args  # the bind owns it
    assert plan.host_paths_mounted and plan.host_cwd == str(work)


def test_cwd_not_bound_by_default(tmp_path: Path, work: Path) -> None:
    plan = _plan(tmp_path, BASE, host_cwd=str(work))
    assert plan.cwd_target is None and plan.default_cwd == "/workspace"
    assert plan.volume_args == ()
    assert not plan.host_paths_mounted


def test_cwd_goes_to_host_cwd_when_workspace_is_taken(tmp_path: Path, work: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    settings = replace(
        BASE,
        mount_cwd_to_workspace=True,
        volumes=(f"{data}:/workspace", f"{data}:/host-cwd"),
    )
    plan = _plan(tmp_path, settings, host_cwd=str(work))
    assert plan.cwd_target == "/host-cwd-2"
    assert _pairs(plan.volume_args, "-v")[0] == f"{work}:/host-cwd-2"


def test_existing_mount_of_the_cwd_is_reused(tmp_path: Path, work: Path) -> None:
    settings = replace(BASE, mount_cwd_to_workspace=True, volumes=(f"{work}:/src:rw",))
    plan = _plan(tmp_path, settings, host_cwd=str(work))
    assert plan.cwd_target == "/src"
    assert _pairs(plan.volume_args, "-v") == [f"{work}:/src:rw"]  # no second bind


def test_windows_drive_cwd_is_always_bound(tmp_path: Path) -> None:
    plan = _plan(tmp_path, BASE, host_cwd="C:\\Users\\me\\proj")
    assert plan.cwd_target == "/workspace"
    assert _pairs(plan.volume_args, "-v") == ["C:\\Users\\me\\proj:/workspace"]
    assert split_volume("C:\\Users\\me:/w:ro") == ("C:\\Users\\me", "/w", "ro")


def test_malformed_volumes_are_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    good = tmp_path / "good"
    good.mkdir()
    settings = replace(BASE, volumes=("no-colon", "relative:path", "", f"{good}:/data:ro"))
    plan = _plan(tmp_path, settings)
    assert _pairs(plan.volume_args, "-v") == [f"{good}:/data:ro"]
    assert caplog.text.count("skipped") == 2


def test_home_in_volume_is_the_scope_home(tmp_path: Path) -> None:
    plan = _plan(tmp_path, replace(BASE, volumes=("~/data:/data",)), user_home="/home/someone")
    assert _pairs(plan.volume_args, "-v") == ["/home/someone/data:/data"]


def test_host_paths_mounted_only_for_writable_host_binds(tmp_path: Path) -> None:
    assert not _plan(tmp_path, replace(BASE, volumes=("named-volume:/cache",))).host_paths_mounted
    assert not _plan(tmp_path, replace(BASE, volumes=("/srv/ref:/ref:ro",))).host_paths_mounted
    assert _plan(tmp_path, replace(BASE, volumes=("/srv/out:/out",))).host_paths_mounted
    assert _plan(tmp_path, replace(BASE, volumes=("/srv/out:/out:rw",))).host_paths_mounted


def test_non_persistent_uses_tmpfs(tmp_path: Path) -> None:
    plan = _plan(tmp_path, BASE)
    assert _pairs(plan.writable_args, "--tmpfs") == [
        "/workspace:rw,exec,size=10g",
        "/home:rw,exec,size=1g",
        "/root:rw,exec,size=1g",
    ]
    assert plan.home_dir is None and not (tmp_path / "home" / "sandboxes").exists()


def test_persistent_binds_owner_only_dirs(tmp_path: Path) -> None:
    plan = _plan(tmp_path, replace(BASE, persistent=True))
    sandbox = tmp_path / "home" / "sandboxes" / "docker" / "task"
    assert _pairs(plan.writable_args, "-v") == [
        f"{sandbox / 'home'}:/root",
        f"{sandbox / 'workspace'}:/workspace",
    ]
    for part in ("home", "workspace"):
        assert stat.S_IMODE((sandbox / part).stat().st_mode) == 0o700
    assert not plan.host_paths_mounted  # Indus's own scratch space, not the user's files


def test_task_key_with_colons_is_sanitised(tmp_path: Path) -> None:
    name = sandbox_dir_name("cli:session:20261018_120000_abcdef")
    assert ":" not in name and name.startswith("cli_session_20261018_120000_abcdef-")
    assert sandbox_dir_name("a:b") != sandbox_dir_name("a_b")
    assert sandbox_dir_name("plain-key_1") == "plain-key_1"
    assert sandbox_dir_name("") == "default"
    assert sandbox_dir_name("..") != ".."
    plan = _plan(tmp_path, replace(BASE, persistent=True), task_key="a:b")
    for spec in _pairs(plan.writable_args, "-v"):
        assert spec.count(":") == 1  # host:container, nothing for -v to misread


def test_readonly_mounts(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    token = tmp_path / "token.json"
    token.write_text("{}")
    mounts = [
        ReadonlyMount(str(skills), "/root/.indus/skills", "dir"),
        ReadonlyMount(str(token), "/root/.indus/credentials/token.json", "file"),
        ReadonlyMount(str(tmp_path / "missing"), "/x", "dir"),
        ReadonlyMount(str(skills), "/y", "file"),  # wrong kind: skipped
    ]
    plan = _plan(tmp_path, BASE, readonly=mounts)
    assert _pairs(plan.readonly_args, "-v") == [
        f"{skills}:/root/.indus/skills:ro",
        f"{token}:/root/.indus/credentials/token.json:ro",
    ]
    assert not plan.host_paths_mounted
    assert plan.args[-4:] == plan.readonly_args


@pytest.fixture
def registered() -> Iterator[list[ReadonlyMount]]:
    found: list[ReadonlyMount] = []

    def source(scope: Scope) -> list[ReadonlyMount]:
        return found

    def broken(scope: Scope) -> list[ReadonlyMount]:
        raise RuntimeError("boom")

    register_readonly_source(source)
    register_readonly_source(source)
    register_readonly_source(broken)
    yield found
    unregister_readonly_source(source)
    unregister_readonly_source(broken)


def test_registered_sources(registered: list[ReadonlyMount], tmp_path: Path) -> None:
    registered.append(ReadonlyMount("/a", "/b"))
    scope = make_scope(tmp_path / "h")
    assert readonly_from_sources(scope) == [ReadonlyMount("/a", "/b")]


def test_credential_files_come_from_the_registry(tmp_path: Path) -> None:
    from indus.backends.docker.mounts import credential_mounts
    from tests.fakes.terminal import terminal_scope

    user = tmp_path / "user"
    token = user / ".config" / "tool" / "token.json"
    token.parent.mkdir(parents=True)
    token.write_text("{}")
    token.chmod(0o600)
    scope = terminal_scope(
        tmp_path, {"terminal": {"credential_files": ["~/.config/tool/token.json", "~/gone"]}}
    )
    scope = scope.with_env(scope.env.with_overlay({"HOME": str(user)}))
    found = credential_mounts(scope, "s1")
    assert found == [
        ReadonlyMount(str(token), "/root/.indus/credentials/.config/tool/token.json", "file"),
    ]
    plan = _plan(tmp_path, BASE, readonly=found)
    assert all(spec.endswith(":ro") for spec in _pairs(plan.readonly_args, "-v"))
    assert not plan.host_paths_mounted


def test_unlisted_credential_files_mean_no_mounts(tmp_path: Path) -> None:
    from indus.backends.docker.mounts import credential_mounts

    assert credential_mounts(make_scope(tmp_path / "h")) == []


def test_media_cache_is_mounted_where_the_media_tools_say(tmp_path: Path) -> None:
    from indus.backends.docker.environment import MEDIA_TARGET, media_mounts
    from indus.tools.builtin.media.common import sandbox_path
    from tests.fakes.agent import DictConfig

    home = tmp_path / "profile"
    home.mkdir()
    scope = make_scope(home, env={"HOME": str(home)})
    (mount,) = media_mounts(scope)  # created when missing, so later files show up
    assert Path(mount.host_path) == home / "cache" / "media" and Path(mount.host_path).is_dir()
    plan = _plan(tmp_path, BASE, readonly=[mount])
    assert _pairs(plan.readonly_args, "-v") == [f"{mount.host_path}:{MEDIA_TARGET}:ro"]
    assert not plan.host_paths_mounted
    made = Path(mount.host_path) / "image-1.png"
    docker = scope.with_config(DictConfig({"terminal": {"backend": "docker"}}))
    assert sandbox_path(docker, made) == f"{MEDIA_TARGET}/image-1.png"
