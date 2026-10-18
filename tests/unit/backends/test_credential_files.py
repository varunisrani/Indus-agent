"""Credential files registry (P11-11)."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.backends import credential_files as cf
from indus.backends.remote_sync import credential_items
from indus.foundation.scope import Scope
from tests.fakes.terminal import terminal_scope


@pytest.fixture(autouse=True)
def fresh_registry() -> Iterator[None]:
    cf._declared.clear()  # pyright: ignore[reportPrivateUsage]
    cf._warned.clear()  # pyright: ignore[reportPrivateUsage]
    yield
    cf._declared.clear()  # pyright: ignore[reportPrivateUsage]
    cf._warned.clear()  # pyright: ignore[reportPrivateUsage]


def scope_with(tmp_path: Path, files: list[str]) -> Scope:
    scope = terminal_scope(tmp_path, {"terminal": {"credential_files": files}})
    user_home = tmp_path / "user"
    user_home.mkdir(exist_ok=True)
    return scope.with_env(scope.env.with_overlay({"HOME": str(user_home)}))


def put(path: Path, text: str = "token") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)
    return path


def test_registry_table(tmp_path: Path) -> None:
    user = tmp_path / "user"
    put(user / ".config" / "gcloud" / "creds.json")
    outside = put(tmp_path / "shared" / "svc.key")
    scope = scope_with(
        tmp_path, ["~/.config/gcloud/creds.json", str(outside), "~/.config/gcloud/creds.json"]
    )
    found = cf.resolve(scope, "s1")
    assert [f.remote_rel for f in found] == [
        "credentials/.config/gcloud/creds.json",
        "credentials/abs/" + str(outside).lstrip("/"),
    ]
    assert all(f.mode == 0o600 and os.path.isabs(f.host_path) for f in found)


def test_secret_stores_are_refused(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    scope = scope_with(tmp_path, [])
    store = put(scope.home / "secrets.json", "{}")
    dotenv = put(scope.home / ".env", "A=1")
    project_env = put(tmp_path / "proj" / ".env.production", "B=2")
    # A link into the secret store is refused as well.
    link = tmp_path / "user" / "innocent.json"
    link.symlink_to(store)
    scope = scope_with(tmp_path, [str(store), str(dotenv), str(project_env), str(link)])
    with caplog.at_level(logging.WARNING):
        assert cf.resolve(scope, "s1") == []
    assert sum(": refused:" in r.getMessage() for r in caplog.records) == 4


def test_missing_file_warned_once_per_session(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    scope = scope_with(tmp_path, ["~/nope.json"])
    with caplog.at_level(logging.WARNING):
        assert cf.resolve(scope, "s1") == []
        assert cf.resolve(scope, "s1") == []
        assert cf.resolve(scope, "s2") == []
    assert sum("nope.json" in r.getMessage() for r in caplog.records) == 2  # s1 and s2
    cf.clear(scope, "s1")
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        cf.resolve(scope, "s1")
    assert "nope.json" in caplog.text  # a new session warns again


def test_other_owner_and_directories_refused(tmp_path: Path) -> None:
    mine = put(tmp_path / "user" / "mine.json")
    folder = tmp_path / "user" / "folder"
    folder.mkdir()
    scope = scope_with(tmp_path, [str(mine), str(folder)])
    assert [f.host_path for f in cf.resolve(scope, "s1")] == [str(mine.resolve())]
    assert cf.resolve(scope, "s1", uid=os.getuid() + 1) == []


def test_skill_declared_files_are_per_session(tmp_path: Path) -> None:
    put(tmp_path / "user" / ".aws" / "token")
    scope = scope_with(tmp_path, [])
    cf.register(scope, "s1", ["~/.aws/token"], mode=0o400)
    assert [(f.remote_rel, f.mode) for f in cf.resolve(scope, "s1")] == [
        ("credentials/.aws/token", 0o400)
    ]
    assert cf.resolve(scope, "s2") == []
    cf.clear(scope, "s1")
    assert cf.resolve(scope, "s1") == []


def test_docker_mounts_read_only_and_sync_upload_only(tmp_path: Path) -> None:
    put(tmp_path / "user" / "svc.json")
    scope = scope_with(tmp_path, ["~/svc.json"])
    found = cf.resolve(scope, "s1")
    host = str((tmp_path / "user" / "svc.json").resolve())
    assert cf.mount_specs(found, "/root/.indus") == [f"{host}:/root/.indus/credentials/svc.json:ro"]
    items = credential_items(found, "/home/dev/.indus")
    assert [(i.remote_path, i.upload_only, i.mode) for i in items] == [
        ("/home/dev/.indus/credentials/svc.json", True, 0o600)
    ]
