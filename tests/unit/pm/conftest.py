"""Shared pm fixtures: a fake origin, a tiny package list and a ``Pm`` wired to both."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from indus.pm.core import Package, Pm, parse_definitions
from indus.pm.lock import Artifact, Lock, Pin
from tests.fakes.clock import FakeClock
from tests.fakes.pm_origin import MIRROR, FakeOrigin, definitions_text, sha256, tar_gz

TARGET = "linux-x64"
TOOL_SCRIPT = b"#!/bin/sh\necho tool-1.0\n"


def tool_rows(version: str = "1.0", **extra: object) -> dict[str, object]:
    return {
        "summary": "a test tool",
        "kind": "required",
        "version": version,
        "url": "https://dl.example/tool/{version}/tool-{plat}.tar.gz",
        "format": "tar.gz",
        "strip": 1,
        "exe": "bin/tool",
        "path": ["bin"],
        "releases": "github:example/tool",
        "targets": {TARGET: {"plat": "linux-x64"}, "darwin-arm64": {"plat": "darwin-arm64"}},
        **extra,
    }


def extra_rows() -> dict[str, object]:
    return {
        "summary": "a default extra",
        "kind": "default",
        "version": "2.0",
        "url": "https://dl.example/extra/extra-{version}",
        "format": "file",
        "exe": "extra",
        "releases": "pinned",
        "targets": {"any": {}},
    }


def standard_packages() -> dict[str, Package]:
    return parse_definitions(definitions_text({"tool": tool_rows(), "extra": extra_rows()}))


def tool_archive(version: str = "1.0") -> bytes:
    return tar_gz({"bin/tool": TOOL_SCRIPT.replace(b"1.0", version.encode()), "README": b"hi"})


@pytest.fixture
def origin() -> FakeOrigin:
    return FakeOrigin()


@pytest.fixture
def pm_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    return root


@pytest.fixture
def make_pm(
    pm_root: Path, origin: FakeOrigin, fake_clock: FakeClock
) -> Iterator[Callable[..., Pm]]:
    made: list[Pm] = []

    def build(
        packages: dict[str, Package] | None = None, root: Path | None = None, **kw: object
    ) -> Pm:
        settings: dict[str, object] = {
            "env": {"PATH": "/usr/bin"},
            "target": TARGET,
            "packages": packages if packages is not None else standard_packages(),
            "mirror": MIRROR,
            "transport": origin.transport(),
            "clock": fake_clock,
        }
        settings.update(kw)
        pm = Pm(root or pm_root, **settings)  # type: ignore[arg-type]
        made.append(pm)
        return pm

    yield build
    for pm in made:
        pm.close()


def publish_tool(origin: FakeOrigin, version: str = "1.0") -> Artifact:
    data = tool_archive(version)
    url = origin.add(f"tool/{version}/tool-linux-x64.tar.gz", data)
    return Artifact(url, sha256(data))


def publish_extra(origin: FakeOrigin) -> Artifact:
    data = b"#!/bin/sh\necho extra\n"
    url = origin.add("extra/extra-2.0", data)
    return Artifact(url, sha256(data))


def write_lock(pm: Pm, pins: dict[str, Pin]) -> None:
    Lock(pins).write(pm.paths.lock)


@pytest.fixture
def locked_pm(make_pm: Callable[..., Pm], origin: FakeOrigin) -> Pm:
    """A pm whose lock pins ``tool`` 1.0 (linux-x64) and ``extra`` 2.0 (any)."""
    pm = make_pm()
    write_lock(
        pm,
        {
            "tool": Pin("1.0", {TARGET: publish_tool(origin)}),
            "extra": Pin("2.0", {"any": publish_extra(origin)}),
        },
    )
    return pm
