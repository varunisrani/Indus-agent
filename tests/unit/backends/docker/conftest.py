"""Shared helpers for the Docker backend tests: a fake client on PATH and scopes that use it."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Literal

import pytest

from indus.backends.docker import probe, reaper
from indus.backends.docker.environment import DockerEnvironment, open_docker
from indus.backends.registry import CreateRequest, EnvKey
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope, make_scope
from tests.fakes.backend.docker_cli import FakeDocker
from tests.fakes.clock import FakeClock

IMAGE = "registry.test/indus-sandbox:test"


class Config:
    """A config view over dotted keys."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        self.values = dict(values)

    def get(self, path: str, /) -> object:
        return self.values.get(path)


class Secrets:
    """A secret source whose values tests change between calls."""

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self.values = dict(values or {})

    def get(self, name: str, /) -> str | None:
        return self.values.get(name)

    def known_values(self) -> list[str]:
        return list(self.values.values())

    @property
    def generation(self) -> int:
        return len(self.values)


# The fake client runs container commands on this machine, so a container's /tmp is the host's:
# snapshot files of environments opened by a test are removed after it.
_opened: list[DockerEnvironment] = []


@pytest.fixture(autouse=True)
def fresh_probe_caches() -> Iterator[None]:
    probe.clear_caches()
    reaper.reset_for_tests()
    yield
    probe.clear_caches()
    reaper.reset_for_tests()
    while _opened:
        snapshot = Path(_opened.pop().snapshot_path)
        for path in [snapshot, *snapshot.parent.glob(snapshot.name + ".tmp.*")]:
            path.unlink(missing_ok=True)


def track(env: DockerEnvironment) -> DockerEnvironment:
    _opened.append(env)
    return env


@pytest.fixture
def fake_docker(tmp_path: Path) -> FakeDocker:
    fake = FakeDocker(tmp_path / "fake-docker")
    fake.install()
    fake.add_image(IMAGE)
    return fake


def docker_scope(
    tmp_path: Path,
    fake: FakeDocker | None,
    clock: FakeClock,
    *,
    profile: str = "work",
    env: Mapping[str, str] | None = None,
    secrets: Secrets | None = None,
    **config: Any,
) -> Scope:
    """A profile whose PATH finds the fake client and whose config picks Docker."""
    home = tmp_path / "root" / "profiles" / profile
    home.mkdir(parents=True, exist_ok=True)
    user_home = tmp_path / "user-home"
    user_home.mkdir(exist_ok=True)
    path = f"{fake.bin_dir}:/usr/bin:/bin" if fake is not None else "/nonexistent"
    view = {"PATH": path, "HOME": str(user_home), **(env or {})}
    values: dict[str, Any] = {
        "terminal.backend": "docker",
        "terminal.docker_image": IMAGE,
        "terminal.timeout": 30,
    }
    values.update({k.replace("__", "."): v for k, v in config.items()})
    scope = make_scope(home, env=view, clock=clock).with_config(Config(values))
    return scope.with_secrets(secrets) if secrets is not None else scope


def request_for(
    scope: Scope,
    *,
    cwd: str | None = None,
    kind: Literal["task", "session", "profile", "host"] = "profile",
    ident: str = "shared",
    session_id: str | None = None,
) -> CreateRequest:
    key = EnvKey(str(scope.home), kind, ident, "docker")
    return CreateRequest(scope, key, None, session_id, {"cwd": cwd} if cwd else {})


def _no_reaper(*_: object) -> None:
    return None


async def open_env(
    scope: Scope,
    fake: FakeDocker,
    cwd: Path | None = None,
    *,
    clock: Clock | None = None,
    **request: Any,
) -> DockerEnvironment:
    """A started Docker environment over the fake client (the orphan sweep left out)."""
    return track(
        await open_docker(
            request_for(scope, cwd=str(cwd) if cwd else None, **request),
            runner=fake.runner,
            os_name="linux",
            clock=clock,
            reaper=_no_reaper,
        )
    )
