"""Per-task image / backend / cwd overrides (P21-14)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.backends import task_overrides
from indus.backends.contract import BackendFlags, Environment
from indus.backends.registry import BackendEntry, CreateRequest, EnvironmentRegistry
from indus.backends.task_overrides import (
    TASK_OVERRIDES,
    LayeredConfig,
    TaskOverride,
    plan,
    register_row,
)
from indus.foundation.scope import Scope
from tests.fakes.backend.environment import FakeEnvironment
from tests.fakes.terminal import terminal_scope
from tests.unit.backends._aio import aio

IMAGE = "registry.example/team/img:1"


@pytest.fixture(autouse=True)
def fresh() -> Iterator[None]:
    TASK_OVERRIDES.reset()
    task_overrides.reset_warnings()
    yield
    TASK_OVERRIDES.reset()
    task_overrides.reset_warnings()


def _scope(tmp_path: Path, backend: str = "local", *, name: str = "home") -> Scope:
    return terminal_scope(tmp_path, {"terminal": {"backend": backend}}, name=name)


# -- the mapping table ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("backend", "config_key", "config_value", "call_image"),
    [
        ("docker", "terminal.docker_image", IMAGE, None),
        ("singularity", "terminal.singularity_image", f"docker://{IMAGE}", None),
        ("modal", None, None, IMAGE),
        ("daytona", None, None, IMAGE),
        ("vercel_sandbox", None, None, None),
        ("local", None, None, None),
        ("ssh", None, None, None),
    ],
)
def test_image_mapping_per_backend(
    tmp_path: Path,
    backend: str,
    config_key: str | None,
    config_value: str | None,
    call_image: str | None,
) -> None:
    scope = _scope(tmp_path, backend)
    planned = plan(scope, "task_7", TaskOverride(image=IMAGE))
    assert planned.overrides.get("image") == call_image
    assert planned.overrides["task"] == "task_7"
    if config_key is None:
        assert planned.scope is scope
    else:
        assert planned.scope.config is not None
        assert planned.scope.config.get(config_key) == config_value
        assert planned.scope.config.get("terminal.backend") == backend  # the rest shines through
        assert scope.config is not None and scope.config.get(config_key) != config_value


def test_a_singularity_ref_with_a_scheme_is_kept(tmp_path: Path) -> None:
    scope = _scope(tmp_path, "singularity")
    planned = plan(scope, "t", TaskOverride(image="library://x/y:1"))
    assert planned.scope.config is not None
    assert planned.scope.config.get("terminal.singularity_image") == "library://x/y:1"


def test_backend_and_cwd_become_call_overrides(tmp_path: Path) -> None:
    scope = _scope(tmp_path, "local")
    planned = plan(scope, "t", TaskOverride(image=IMAGE, backend="Docker", cwd="/work/a"))
    assert planned.overrides == {"task": "t", "backend": "docker", "cwd": "/work/a"}
    assert planned.scope.config is not None
    assert planned.scope.config.get("terminal.docker_image") == IMAGE


def test_ignored_images_warn_once_per_backend(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    scope = _scope(tmp_path, "vercel_sandbox")
    with caplog.at_level(logging.WARNING, logger="indus.backends.task_overrides"):
        plan(scope, "a", TaskOverride(image=IMAGE))
        plan(scope, "b", TaskOverride(image=IMAGE))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "vercel_sandbox" in warnings[0].getMessage()


@pytest.mark.parametrize("pin", [True, "yes"])
def test_a_pinned_docker_image_cannot_be_lifted_by_a_task(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, pin: object
) -> None:
    pinned = "registry.example/team/base@sha256:" + "a" * 64
    config = {"terminal": {"backend": "docker", "docker_image": pinned, "docker_image_pinned": pin}}
    scope = terminal_scope(tmp_path, config)
    digest_image = "registry.example/other/img@sha256:" + "b" * 64
    with caplog.at_level(logging.WARNING, logger="indus.backends.task_overrides"):
        for task, image in (("a", IMAGE), ("b", digest_image)):
            planned = plan(scope, task, TaskOverride(image=image, cwd="/w"))
            assert planned.scope is scope  # no overlay: the pinned image stays
            assert planned.overrides == {"task": task, "cwd": "/w"}  # still its own sandbox
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "docker_image_pinned" in warnings[0].getMessage()


def test_layered_config_forwards_attributes(tmp_path: Path) -> None:
    class Broken:
        failed = True

        def get(self, path: str, /) -> object:
            return None

    layered = LayeredConfig(Broken(), {"terminal.docker_image": IMAGE})
    assert layered.failed is True
    assert layered.get("terminal.docker_image") == IMAGE
    assert layered.get("terminal.backend") is None


# -- the registry ---------------------------------------------------------------------------------


def test_profile_scoping_and_clear(tmp_path: Path) -> None:
    a = _scope(tmp_path, name="a")
    b = _scope(tmp_path, name="b")
    TASK_OVERRIDES.set(a, "task_1", TaskOverride(image=IMAGE))
    TASK_OVERRIDES.bind(a, "sess-1", "task_1")
    assert TASK_OVERRIDES.resolve(a, "task_1") == ("task_1", TaskOverride(image=IMAGE))
    assert TASK_OVERRIDES.resolve(a, "sess-1") == ("task_1", TaskOverride(image=IMAGE))
    assert TASK_OVERRIDES.resolve(b, "task_1") is None  # R02.8
    assert TASK_OVERRIDES.resolve(b, "sess-1") is None
    assert TASK_OVERRIDES.clear(a, "task_1") is True
    assert TASK_OVERRIDES.resolve(a, "sess-1") is None
    assert TASK_OVERRIDES.clear(a, "task_1") is False
    assert len(TASK_OVERRIDES) == 0


def test_register_row_skips_empty_rows(tmp_path: Path) -> None:
    scope = _scope(tmp_path)
    assert register_row(scope, "task_0", None, "  ") is None
    assert TASK_OVERRIDES.get(scope, "task_0") is None
    assert register_row(scope, "task_1", f" {IMAGE} ", "/w") is None
    assert TASK_OVERRIDES.get(scope, "task_1") == TaskOverride(image=IMAGE, cwd="/w")


# -- environment keys and factories ---------------------------------------------------------------


class Recorder:
    """Factories that record their request and hand out fake environments."""

    def __init__(self) -> None:
        self.requests: list[CreateRequest] = []

    def entry(self, name: str) -> BackendEntry:
        async def factory(request: CreateRequest) -> Environment:
            self.requests.append(request)
            return FakeEnvironment()

        return BackendEntry(name, factory, BackendFlags(isolated=True, host_local=False))


def _registry(recorder: Recorder) -> EnvironmentRegistry:
    names = ("local", "docker", "singularity", "modal", "daytona", "vercel_sandbox")
    return EnvironmentRegistry([recorder.entry(name) for name in names])


def test_an_override_gets_its_own_env_key(tmp_path: Path) -> None:
    scope = _scope(tmp_path, "docker")
    registry = _registry(Recorder())
    default = registry.key_for(scope, session_id="s1")
    TASK_OVERRIDES.set(scope, "task_3", TaskOverride(image=IMAGE))
    by_task = registry.key_for(scope, task_id="task_3", session_id="s1")
    assert by_task != default
    assert (by_task.kind, by_task.ident, by_task.backend) == ("task", "task_3", "docker")
    TASK_OVERRIDES.bind(scope, "s1", "task_3")
    assert registry.key_for(scope, session_id="s1") == by_task
    TASK_OVERRIDES.clear(scope, "task_3")
    assert registry.key_for(scope, session_id="s1") == default


def test_an_image_ignored_on_vercel_still_keys_per_task(tmp_path: Path) -> None:
    scope = _scope(tmp_path, "vercel_sandbox")
    registry = _registry(Recorder())
    TASK_OVERRIDES.set(scope, "task_1", TaskOverride(image=IMAGE))
    key = registry.key_for(scope, task_id="task_1")
    assert (key.kind, key.ident) == ("task", "task_1")


def test_explicit_overrides_win(tmp_path: Path) -> None:
    scope = _scope(tmp_path, "docker")
    registry = _registry(Recorder())
    TASK_OVERRIDES.set(scope, "task_1", TaskOverride(backend="modal"))
    key = registry.key_for(scope, task_id="task_1", overrides={"backend": "daytona"})
    assert key.backend == "daytona"


@pytest.mark.parametrize(
    ("backend", "check"),
    [
        ("docker", lambda r: r.scope.config.get("terminal.docker_image") == IMAGE),
        (
            "singularity",
            lambda r: r.scope.config.get("terminal.singularity_image") == f"docker://{IMAGE}",
        ),
        ("modal", lambda r: r.overrides.get("image") == IMAGE),
        ("daytona", lambda r: r.overrides.get("image") == IMAGE),
        ("vercel_sandbox", lambda r: "image" not in r.overrides),
    ],
)
@aio
async def test_the_factory_gets_the_task_image(tmp_path: Path, backend: str, check: Any) -> None:
    scope = _scope(tmp_path, backend)
    recorder = Recorder()
    registry = _registry(recorder)
    TASK_OVERRIDES.set(scope, "task_2", TaskOverride(image=IMAGE, cwd="/work/t2"))
    TASK_OVERRIDES.bind(scope, "sess", "task_2")
    try:
        key, _env = await registry.acquire(scope, session_id="sess")
        assert (key.kind, key.ident) == ("task", "task_2")
        [request] = recorder.requests
        assert request.task_id == "task_2" and request.session_id == "sess"
        assert request.overrides.get("cwd") == "/work/t2"
        assert check(request)
        await registry.acquire(scope, session_id="other")  # the default sandbox is separate
        assert len(recorder.requests) == 2
        assert recorder.requests[1].overrides == {}
        assert recorder.requests[1].key.kind != "task"
        assert await registry.release(scope, session_id="sess") is True
        assert key not in registry.live()
    finally:
        await registry.shutdown(fence=False)


@aio
async def test_a_backend_override_picks_another_row(tmp_path: Path) -> None:
    scope = _scope(tmp_path, "local")
    recorder = Recorder()
    registry = _registry(recorder)
    TASK_OVERRIDES.set(scope, "task_9", TaskOverride(backend="docker", image=IMAGE))
    try:
        key, _env = await registry.acquire(scope, task_id="task_9")
        assert key.backend == "docker"
        assert recorder.requests[0].scope.config is not None
        assert recorder.requests[0].scope.config.get("terminal.docker_image") == IMAGE
    finally:
        await registry.shutdown(fence=False)
