"""P07-22: `indus backup` — exclusions (R04.23), safe database copies, keep-N, one at a time."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import sqlite3
import zipfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import pytest

from indus.cli import backup_rules
from indus.cli.commands import backup as backup_mod
from indus.foundation.scope import Scope, make_scope
from tests.unit.cli.commands._helpers_d import (  # noqa: F401
    aged,
    fake_clock,
    run,
    seeded,
    store_globals,
)


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@contextlib.contextmanager
def _socket_at(path: Path) -> Iterator[bool]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
    except OSError:
        sock.close()
        yield False
        return
    try:
        yield True
    finally:
        sock.close()


KEPT = {
    "config.yaml",
    ".env",
    "PERSONA.md",
    "profiles/work/config.yaml",
    "profiles/work/profile.yaml",
    "profiles/work/state.db",
    "skills/demo/models/notes.txt",
    "cache/images/pic.png",
    "profiles/work/cache/audio/clip.ogg",
    "memories/MEMORY.md",
}


def _home_with_everything(scope: Scope) -> Path:
    root = scope.root
    with seeded(scope) as store:
        aged(store, "s1", 1, title="kept talk")
    for rel in KEPT - {"profiles/work/state.db"}:
        _write(root / rel, f"content of {rel}")
    for rel in (
        ".git/HEAD",
        "skills/demo/__pycache__/m.cpython-313.pyc",
        "node_modules/pkg/index.js",
        "backups/config/config.yaml.bak",
        "state-snapshots/20260101-000000/config.yaml",
        "checkpoints/store/objects/aa",
        "browser-profiles/Default/Cookies",
        "profiles/work/odd-name/Default/Login Data",
        ".venv/bin/python",
        "models/big.bin",
        "profiles/work/runtimes/node.tar",
        "cache/catalog/models.json",
        "cache/loose.json",
        "profiles/work/kanban.db-wal",
        "profiles/work/kanban.db-shm",
        "profiles/work/old.sqlite-journal",
        "gateway.pid",
        "profiles/work/config.yaml.lock",
        "state.db.retired-wal-20260101-1/manifest.json",
        "skills/demo/thing.pyc",
    ):
        _write(root / rel)
    (root / "outside-link").symlink_to("/etc/hostname")
    (root / "linked-dir").symlink_to(root / "skills")
    return root


def _names(zip_path: Path) -> set[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return {n for n in zf.namelist() if not n.endswith("/")}


def test_every_excluded_kind_stays_out(scope: Scope, tmp_path: Path) -> None:
    root = _home_with_everything(scope)
    out_dir = tmp_path / "out"
    with _socket_at(root / "s.sock"):
        result = run(scope, "backup", "-o", str(out_dir) + "/")
    assert result.code == 0, result.err
    (archive,) = out_dir.glob("indus-backup-*.zip")
    assert archive.name == "indus-backup-2026-01-01-090000.zip"
    assert _names(archive) == KEPT
    assert oct(archive.stat().st_mode & 0o777) == "0o600"
    assert "Restore with: indus import" in result.out


def test_rules_table() -> None:
    def d(rel: str) -> bool:
        return backup_rules.skip_dir(PurePosixPath(rel), Path("/nonexistent") / rel)

    assert d("profiles/work/node_modules")
    assert d("models") and d("profiles/x/models") and not d("skills/a/models")
    assert d("cache/thumbs") and not d("cache/images") and not d("profiles/x/cache/videos")
    assert d("state.db.retired-wal-1")
    assert not d("skills") and not d("profiles/work")


def test_browser_profile_found_by_content(tmp_path: Path) -> None:
    folder = tmp_path / "whatever"
    _write(folder / "Default" / "Cookies")
    assert backup_rules.looks_like_browser_profile(folder)
    assert not backup_rules.looks_like_browser_profile(tmp_path / "plain")


def test_sidecars_need_a_database_name(tmp_path: Path) -> None:
    assert backup_rules.is_sidecar(_write(tmp_path / "a.db-wal"))
    assert backup_rules.is_sidecar(_write(tmp_path / "kanban.sqlite-journal"))
    _write(tmp_path / "store")
    assert backup_rules.is_sidecar(_write(tmp_path / "store-shm"))
    assert not backup_rules.is_sidecar(_write(tmp_path / "travel-journal"))


@pytest.mark.parametrize("journal", ["delete", "wal"])
def test_database_copied_while_a_writer_holds_a_transaction(
    scope: Scope, tmp_path: Path, journal: str
) -> None:
    db = scope.root / "data.db"
    writer = sqlite3.connect(db, isolation_level=None)
    writer.execute(f"PRAGMA journal_mode={journal}")
    writer.execute("CREATE TABLE t (x INTEGER)")
    writer.execute("INSERT INTO t VALUES (1)")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO t VALUES (2)")
    try:
        result = run(scope, "backup", "-o", str(tmp_path / "b.zip"))
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert result.code == 0, result.err
    names = _names(tmp_path / "b.zip")
    assert "data.db" in names
    assert not any(n.endswith(("-wal", "-shm")) for n in names)
    extracted = tmp_path / "copy.db"
    with zipfile.ZipFile(tmp_path / "b.zip") as zf:
        extracted.write_bytes(zf.read("data.db"))
    with sqlite3.connect(extracted) as conn:
        assert conn.execute("SELECT x FROM t").fetchall() == [(1,)]


def test_keep_prunes_only_matching_names(scope: Scope, tmp_path: Path) -> None:
    _write(scope.root / "config.yaml")
    out = tmp_path / "out"
    for stamp in ("2025-01-01-000000", "2025-02-01-000000", "2025-03-01-000000"):
        _write(out / f"indus-backup-{stamp}.zip")
    _write(out / "indus-backup-2025-04-01-000000.incomplete.zip")
    _write(out / "my-own-backup.zip")
    result = run(scope, "backup", "-o", str(out) + "/", "-k", "2")
    assert result.code == 0, result.err
    assert sorted(p.name for p in out.iterdir()) == [
        "indus-backup-2025-03-01-000000.zip",
        "indus-backup-2025-04-01-000000.incomplete.zip",
        "indus-backup-2026-01-01-090000.zip",
        "my-own-backup.zip",
    ]


def test_keep_zero_keeps_everything(scope: Scope, tmp_path: Path) -> None:
    _write(scope.root / "config.yaml")
    out = tmp_path / "out"
    for stamp in ("2025-01-01-000000", "2025-02-01-000000"):
        _write(out / f"indus-backup-{stamp}.zip")
    assert run(scope, "backup", "-o", str(out) + "/", "-k", "0").code == 0
    assert len(list(out.iterdir())) == 3


def test_a_second_backup_exits_2(scope: Scope, tmp_path: Path) -> None:
    _write(scope.root / "config.yaml")
    fd = os.open(scope.root / backup_mod.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run(scope, "backup", "-o", str(tmp_path / "b.zip"))
    finally:
        os.close(fd)
    assert result.code == 2
    assert "Another backup is running" in result.err
    assert not (tmp_path / "b.zip").exists()


def test_unreadable_file_keeps_an_incomplete_zip(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(scope.root / "config.yaml")
    _write(scope.root / "broken.txt")
    real = backup_mod._add_file  # pyright: ignore[reportPrivateUsage]

    def flaky(zf: zipfile.ZipFile, source: Path, rel: PurePosixPath, staging: Path) -> int:
        if rel.name == "broken.txt":
            raise PermissionError("denied for the test")
        return real(zf, source, rel, staging)

    monkeypatch.setattr(backup_mod, "_add_file", flaky)
    result = run(scope, "backup", "-o", str(tmp_path / "b.zip"))
    assert result.code == 1
    assert not (tmp_path / "b.zip").exists()
    kept = tmp_path / "b.incomplete.zip"
    assert _names(kept) == {"config.yaml", "profiles/work/profile.yaml"}
    assert "broken.txt" in result.err


def test_keychain_secrets_are_named_not_exported(scope: Scope, tmp_path: Path) -> None:
    home = scope.home
    _write(home / "secrets.index.json", json.dumps({"names": ["OPENAI_API_KEY", "LOCAL_TOKEN"]}))
    file_value = "file" + "-held-" + "value"
    _write(home / "secrets.json", json.dumps({"secrets": {"LOCAL_TOKEN": file_value}}))
    result = run(scope, "backup", "-o", str(tmp_path / "b.zip"))
    assert result.code == 0, result.err
    assert "OS keychain" in result.out
    assert "profile work: OPENAI_API_KEY" in result.out
    assert "LOCAL_TOKEN" not in result.out.split("OS keychain")[1].splitlines()[1]
    assert "profiles/work/secrets.json" in _names(tmp_path / "b.zip")


def test_nothing_to_back_up(scope: Scope, tmp_path: Path) -> None:
    empty = make_scope(tmp_path / "empty-home", clock=scope.clock)
    empty.home.mkdir()
    result = run(empty, "backup", "-o", str(tmp_path / "b.zip"))
    assert result.code == 0 and "No files" in result.out


def test_default_output_goes_to_the_scopes_home_folder(scope: Scope, tmp_path: Path) -> None:
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    (scope.home / "config.yaml").write_text("_config_version: 1\n")
    mine = scope.with_env(scope.env.with_overlay({"HOME": str(user_home)}))
    result = run(mine, "backup")
    assert result.code == 0, result.err
    assert [p.name for p in user_home.iterdir()] == ["indus-backup-2026-01-01-090000.zip"]


# -- quick snapshot -------------------------------------------------------------------------------


def test_quick_snapshot(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    home = scope.home
    with seeded(scope) as store:
        aged(store, "s1", 1)
    _write(home / "config.yaml", "model: x\n")
    _write(home / "memories" / "MEMORY.md", "remember")
    _write(home / ".env", "A=" + "b" * 40)
    folder = home / backup_mod.QUICK_DIR
    for n in range(backup_mod.QUICK_KEEP):
        (folder / f"2025{n:04d}-000000").mkdir(parents=True)
    monkeypatch.setattr(backup_mod, "QUICK_MAX_BYTES", 30)
    result = run(scope, "backup", "--quick", "--label", "before update!")
    assert result.code == 0, result.err
    made = folder / "20260101-090000-before-update"
    assert (made / "config.yaml").read_text() == "model: x\n"
    assert (made / "memories" / "MEMORY.md").is_file()
    assert not (made / ".env").exists()  # over the (patched) size limit
    assert "skipped .env" in result.out
    assert not (made / "state.db").exists()  # the store is larger than 30 bytes too
    snapshots = [p for p in folder.iterdir() if not p.name.startswith(".")]
    assert len(snapshots) == backup_mod.QUICK_KEEP
    assert not (folder / "20250000-000000").exists()


def test_quick_snapshot_copies_the_store(scope: Scope) -> None:
    with seeded(scope) as store:
        aged(store, "s1", 1)
    result = run(scope, "backup", "--quick")
    assert result.code == 0, result.err
    (made,) = (scope.home / backup_mod.QUICK_DIR).iterdir()
    with sqlite3.connect(made / "state.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
