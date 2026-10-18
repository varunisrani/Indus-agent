"""The fake ssh client runs "remote" commands on this machine, so the remote temp dir would be the
host's /tmp. ``remote_tmp`` points the SSH backend's temp dir at the test's own directory instead,
so snapshots, exec pid files and background job files never land in the shared /tmp (another
process's files there are never touched)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fakes.backend.ssh_cli import redirect_remote_tmp


@pytest.fixture(autouse=True)
def remote_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return redirect_remote_tmp(monkeypatch, tmp_path / "remote-tmp")
