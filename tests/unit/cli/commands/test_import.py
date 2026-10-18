"""P07-23: `indus import` — current-home target (R04.22), CRC before write, page-wise DB restore."""

from __future__ import annotations

import os
import sqlite3
import zipfile
from pathlib import Path

import pytest

from indus.cli.commands import import_cmd
from indus.foundation.scope import Scope, make_scope
from indus.state import registry
from tests.unit.cli.commands._helpers_d import (  # noqa: F401
    aged,
    fake_clock,
    run,
    seeded,
    store_globals,
)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.name.endswith(("-wal", "-shm"))
    }


def _zip(path: Path, members: dict[str, bytes | str], *, stored: bool = False) -> Path:
    mode = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(path, "w", mode) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _store_bytes(scope: Scope, tmp_path: Path, sessions: int, name: str = "donor") -> bytes:
    donor = make_scope(tmp_path / name, clock=scope.clock)
    with registry.open_unshared(donor) as store:
        for n in range(sessions):
            aged(store, f"{name}-{n}", 1, title=f"{name} talk {n}")
        path = store.path
    return path.read_bytes()


def _count(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])


def test_restores_into_the_current_profile(scope: Scope, tmp_path: Path) -> None:
    archive = _zip(
        tmp_path / "b.zip",
        {"config.yaml": "model: from-backup\n", ".env": "A=1\n", "skills/x/SKILL.md": "s"},
    )
    result = run(scope, "import", str(archive))
    assert result.code == 0, result.err
    home = scope.home
    assert home == scope.root / "profiles" / "work"
    assert (home / "config.yaml").read_text() == "model: from-backup\n"
    assert (home / "skills" / "x" / "SKILL.md").read_text() == "s"
    assert not (scope.root / "config.yaml").exists()  # never mapped back to the root
    assert oct((home / ".env").stat().st_mode & 0o777) == "0o600"


def test_corrupt_member_leaves_the_home_untouched(scope: Scope, tmp_path: Path) -> None:
    home = scope.home
    (home / "config.yaml").write_text("model: mine\n")
    payload = b"A" * 4000
    archive = _zip(
        tmp_path / "bad.zip",
        {"config.yaml": "model: theirs\n", "notes/big.txt": payload},
        stored=True,
    )
    data = bytearray(archive.read_bytes())
    at = data.index(payload)
    data[at + 100] ^= 0xFF
    archive.write_bytes(bytes(data))
    before = _snapshot(scope.root)
    result = run(scope, "import", str(archive), "-f")
    assert result.code == 1
    assert "damaged" in result.err
    assert _snapshot(scope.root) == before


@pytest.mark.parametrize("member", ["../evil.txt", "/etc/evil.txt", "a/../../evil.txt", "C:/x"])
def test_member_outside_the_home_refuses_everything(
    scope: Scope, tmp_path: Path, member: str
) -> None:
    archive = _zip(tmp_path / "slip.zip", {"config.yaml": "x: 1\n", member: "boom"})
    before = _snapshot(scope.root)
    result = run(scope, "import", str(archive), "-f")
    assert result.code == 1
    assert "outside the home" in result.err
    assert _snapshot(scope.root) == before
    assert not (tmp_path / "evil.txt").exists()


def test_db_written_page_by_page_while_a_reader_is_open(scope: Scope, tmp_path: Path) -> None:
    with seeded(scope) as store:
        aged(store, "mine", 1)
        target = store.path
    inode = target.stat().st_ino
    archive = _zip(tmp_path / "b.zip", {"state.db": _store_bytes(scope, tmp_path, 3)})
    reader = sqlite3.connect(target)
    try:
        assert reader.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        result = run(scope, "import", str(archive))
        assert result.code == 0, result.err
        assert reader.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 3
    finally:
        reader.close()
    assert target.stat().st_ino == inode
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_fewer_sessions_are_reported(scope: Scope, tmp_path: Path) -> None:
    with seeded(scope) as store:
        for n in range(3):
            aged(store, f"mine-{n}", 1)
        target = store.path
    archive = _zip(tmp_path / "b.zip", {"state.db": _store_bytes(scope, tmp_path, 1)})
    result = run(scope, "import", str(archive))
    assert result.code == 0, result.err
    assert "now holds 1 session(s)" in result.out and "held 3" in result.out
    assert _count(target) == 1


def test_damaged_database_member_is_skipped(scope: Scope, tmp_path: Path) -> None:
    with seeded(scope) as store:
        aged(store, "mine", 1)
        target = store.path
    before = target.read_bytes()
    fake = b"SQLite format 3\x00" + b"\x00" * 200
    archive = _zip(tmp_path / "b.zip", {"state.db": fake, "config.yaml": "a: 1\n"})
    result = run(scope, "import", str(archive), "-f")
    assert result.code == 1
    assert "state.db" in result.err
    assert target.read_bytes() == before


def test_runtime_files_of_the_source_machine_are_kept(scope: Scope, tmp_path: Path) -> None:
    home = scope.home
    (home / "gateway.pid").write_text("123")
    archive = _zip(
        tmp_path / "b.zip",
        {
            "config.yaml": "a: 1\n",
            "gateway.pid": "999",
            "processes.json": "{}",
            "state.db-wal": "junk",
            "cron/run.lock": "1",
        },
    )
    result = run(scope, "import", str(archive))
    assert result.code == 0, result.err
    assert (home / "gateway.pid").read_text() == "123"
    assert not (home / "processes.json").exists()
    assert not (home / "state.db-wal").exists()
    assert "Kept this machine's own runtime files (4)" in result.out


def test_existing_setup_needs_force_without_a_terminal(scope: Scope, tmp_path: Path) -> None:
    (scope.home / "config.yaml").write_text("mine: 1\n")
    archive = _zip(tmp_path / "b.zip", {"config.yaml": "theirs: 1\n"})
    refused = run(scope, "import", str(archive))
    assert refused.code == 1 and "-f" in refused.err
    assert (scope.home / "config.yaml").read_text() == "mine: 1\n"
    asked = run(scope, "import", str(archive), tty=True, input="n\n")
    assert asked.code == 0 and (scope.home / "config.yaml").read_text() == "mine: 1\n"
    forced = run(scope, "import", str(archive), "-f")
    assert forced.code == 0 and (scope.home / "config.yaml").read_text() == "theirs: 1\n"


def test_wrapping_folder_is_stripped(scope: Scope, tmp_path: Path) -> None:
    archive = _zip(tmp_path / "b.zip", {".indus/config.yaml": "a: 1\n", ".indus/skills/s.md": "x"})
    result = run(scope, "import", str(archive))
    assert result.code == 0, result.err
    assert (scope.home / "config.yaml").is_file()
    assert (scope.home / "skills" / "s.md").is_file()


def test_prefix_rules() -> None:
    assert import_cmd.strip_prefix([".indus/config.yaml", ".indus/a/b"]) == ".indus/"
    assert import_cmd.strip_prefix(["home/config.yaml", "home/x"]) == "home/"
    assert import_cmd.strip_prefix(["profiles/w/config.yaml", "profiles/w/x"]) == ""
    assert import_cmd.strip_prefix(["config.yaml", "skills/a"]) == ""
    assert import_cmd.strip_prefix(["stuff/a.txt", "stuff/b.txt"]) == ""


@pytest.mark.parametrize(
    ("make", "why"),
    [
        (lambda p: p, "No such file"),
        (lambda p: (p.write_text("plain text"), p)[1], "Not a zip"),
        (lambda p: _zip(p, {}), "empty"),
        (lambda p: _zip(p, {"readme.txt": "hi"}), "does not look like"),
    ],
)
def test_bad_inputs_exit_1(scope: Scope, tmp_path: Path, make: object, why: str) -> None:
    path = tmp_path / "in.zip"
    built = make(path)  # type: ignore[operator]
    result = run(scope, "import", str(built))
    assert result.code == 1 and why in result.err


def test_link_inside_the_home_cannot_redirect_a_member(scope: Scope, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (scope.home / "skills").symlink_to(outside)
    archive = _zip(tmp_path / "b.zip", {"config.yaml": "a: 1\n", "skills/x.md": "x"})
    result = run(scope, "import", str(archive))
    assert result.code == 1
    assert not os.listdir(outside)


def test_backup_taken_while_running_restores_with_the_same_counts(
    scope: Scope, tmp_path: Path
) -> None:
    with seeded(scope) as store:
        for n in range(3):
            aged(store, f"live-{n}", 1, messages=2)
        live_db = store.path
    (scope.home / "config.yaml").write_text("model: {default: m}\n")
    (scope.root / "browser-profiles" / "Default").mkdir(parents=True)
    (scope.root / "browser-profiles" / "Default" / "Cookies").write_text("cookie jar")
    (scope.home / "secrets.index.json").write_text('{"names": ["OPENAI_API_KEY"]}')
    writer = sqlite3.connect(live_db, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE sessions SET title = 'in flight' WHERE id = 'live-0'")
    try:
        made = run(scope, "backup", "-o", str(tmp_path / "full.zip"))
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert made.code == 0, made.err
    with zipfile.ZipFile(tmp_path / "full.zip") as zf:
        names = zf.namelist()
        assert not any("Cookies" in n for n in names)
    # A keychain-held key has no file in the home: it is named in the warning, never archived.
    assert "profile work: OPENAI_API_KEY" in made.out
    assert "profiles/work/secrets.json" not in names
    fresh = make_scope(tmp_path / "fresh-root", clock=scope.clock)
    fresh.home.mkdir()
    restored = run(fresh, "import", str(tmp_path / "full.zip"))
    assert restored.code == 0, restored.err
    assert _count(fresh.home / "profiles" / "work" / "state.db") == 3
    assert (fresh.home / "profiles" / "work" / "config.yaml").is_file()
