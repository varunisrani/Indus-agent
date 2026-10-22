"""``diagnostics.release_folder`` (wave 45, D-16.982): the process's servers of a folder about to
be removed stop first; nothing installed is a no-op; a failure never raises."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from indus.tools.lsp import diagnostics
from indus.tools.lsp.diagnostics import EditDiagnostics, release_folder
from tests.unit.tools.lsp import test_manager as fakes
from tests.unit.tools.lsp._helpers import git_tree


@pytest.fixture(autouse=True)
def fresh_clients() -> None:
    fakes.FakeClient.created = []


def test_nothing_installed_releases_nothing() -> None:
    assert diagnostics.active() is None
    assert asyncio.run(release_folder("/work/tree")) == 0


def test_the_installed_tier_stops_the_folder_s_servers(tmp_path: Path) -> None:
    tree = git_tree(tmp_path / "tree")
    other = git_tree(tmp_path / "other")
    lsp = fakes.manager(tmp_path)
    diagnostics.install_active(EditDiagnostics(lsp))

    async def main() -> int:
        await lsp.client_for(str(tree / "a.fk"))
        await lsp.client_for(str(other / "a.fk"))
        stopped = await release_folder(str(tree))
        await lsp.close()
        return stopped

    try:
        assert asyncio.run(main()) == 1
        assert [c.stopped for c in fakes.FakeClient.created] == [1, 1]  # the other at close
    finally:
        diagnostics.install_active(None)


def test_a_failing_release_is_logged_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lsp = fakes.manager(tmp_path)

    async def boom(folder: str) -> int:
        raise RuntimeError("server hung")

    monkeypatch.setattr(lsp, "release", boom)
    diagnostics.install_active(EditDiagnostics(lsp))
    try:
        assert asyncio.run(release_folder(str(tmp_path))) == 0
    finally:
        diagnostics.install_active(None)
