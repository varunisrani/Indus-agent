"""The auth store (P07-06): read errors vs corruption, lock, metadata only, test isolation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from indus.foundation.errors import (
    AuthIsolationRefused,
    AuthStoreLocked,
    AuthStoreUnreadable,
    AuthUnsafePath,
)
from indus.foundation.fsio import FileLock
from indus.providers.auth.pool_model import PoolEntry, edit_pool, secret_name
from indus.providers.auth.store import AUTH_FILE, AuthStore, empty_document
from indus.state.connection import real_default_root
from tests.fakes.oauth_server import fake_token
from tests.fakes.secret_store import MemorySecretStore

REPO = Path(__file__).resolve().parents[4]


def _seed(home: Path, doc: object) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / AUTH_FILE
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_missing_file_reads_as_empty(tmp_path: Path) -> None:
    assert AuthStore(tmp_path / "h").read() == empty_document()


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX non-root")
def test_os_error_on_read_raises_and_leaves_the_file(tmp_path: Path) -> None:
    path = _seed(tmp_path / "h", {"version": 1, "providers": {"a": {}}, "pool": {}})
    before = path.read_bytes()
    path.chmod(0o000)
    try:
        with pytest.raises(AuthStoreUnreadable) as caught:
            AuthStore(tmp_path / "h").read()
        assert caught.value.code == "auth.unreadable"
    finally:
        path.chmod(0o600)
    assert path.read_bytes() == before
    assert not path.with_name(AUTH_FILE + ".corrupt").exists()


def test_os_error_on_read_raises_any_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _seed(tmp_path / "h", {"version": 1, "providers": {}, "pool": {}})
    before = path.read_bytes()
    real = Path.read_bytes

    def refuse(self: Path) -> bytes:
        if self.name == AUTH_FILE:
            raise OSError(24, "Too many open files")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", refuse)
    store = AuthStore(tmp_path / "h")
    with pytest.raises(AuthStoreUnreadable):
        store.read()
    with pytest.raises(AuthStoreUnreadable), store.transaction():
        pass
    monkeypatch.undo()
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "raw",
    [b'{"version": 1, "providers": {', b"[1, 2, 3]", b"\xff\xfe\x00not utf8", b"null"],
)
def test_corrupt_json_is_copied_aside_and_store_starts_empty(
    tmp_path: Path, raw: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    home = tmp_path / "h"
    home.mkdir()
    (home / AUTH_FILE).write_bytes(raw)
    assert AuthStore(home).read() == empty_document()
    assert (home / (AUTH_FILE + ".corrupt")).read_bytes() == raw
    assert "damaged" in caplog.text


def test_wrong_section_types_are_emptied_per_section(tmp_path: Path) -> None:
    _seed(tmp_path / "h", {"providers": [], "pool": {"x": []}, "active_provider": 3})
    doc = AuthStore(tmp_path / "h").read()
    assert doc["providers"] == {} and doc["pool"] == {"x": []}
    assert "active_provider" not in doc


def test_write_is_owner_only_and_folder_restricted(tmp_path: Path) -> None:
    home = tmp_path / "h"
    store = AuthStore(home)
    with store.transaction() as doc:
        doc["active_provider"] = "openrouter"
    data = json.loads((home / AUTH_FILE).read_text())
    assert data["version"] == 1 and data["active_provider"] == "openrouter"
    if os.name == "posix":
        assert (home / AUTH_FILE).stat().st_mode & 0o777 == 0o600
        assert home.stat().st_mode & 0o777 == 0o700


def test_unchanged_transaction_writes_nothing(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "h")
    with store.transaction():
        pass
    assert not store.path.exists()


def test_secret_never_lands_in_the_json(tmp_path: Path) -> None:
    home = tmp_path / "h"
    store = AuthStore(home)
    secrets = MemorySecretStore()
    key = fake_token("key", 7)
    entry = PoolEntry(id="abcd1234", label="mine")
    secrets.set(secret_name("openrouter", entry.id), key)
    edit_pool(store, "openrouter", lambda pool, _doc: pool.add(entry))
    text = (home / AUTH_FILE).read_text()
    assert key not in text and "abcd1234" in text
    assert secrets.get("AUTH_OPENROUTER_ABCD1234") == key


@pytest.mark.parametrize("field", ["access_token", "refresh_token", "api_key", "key"])
def test_secret_shaped_field_is_refused(tmp_path: Path, field: str) -> None:
    store = AuthStore(tmp_path / "h")
    with pytest.raises(ValueError, match="metadata only"), store.transaction() as doc:
        doc["pool"] = {"p": [{"id": "x", field: fake_token("leak")}]}
    assert not store.path.exists()


def test_lock_timeout_names_the_holder_pid(tmp_path: Path) -> None:
    home = tmp_path / "h"
    home.mkdir()
    ticks = iter(range(0, 1000))

    class Tick:
        def monotonic(self) -> float:
            return float(next(ticks))

    script = textwrap.dedent(
        f"""
        import sys, time
        from indus.foundation.fsio import FileLock
        lock = FileLock({str(home / (AUTH_FILE + ".lock"))!r})
        lock.acquire()
        print("held", flush=True)
        sys.stdin.read()
        """
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=REPO,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        store = AuthStore(home, lock_timeout=3, lock_clock=Tick(), lock_wait=lambda _s: None)
        with pytest.raises(AuthStoreLocked) as caught, store.transaction():
            pass
        assert caught.value.holder_pid == holder.pid
        assert f"process {holder.pid}" in str(caught.value)
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=10)


def test_two_processes_writing_at_once_both_land(tmp_path: Path) -> None:
    home = tmp_path / "h"
    home.mkdir()
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from indus.providers.auth.pool_model import PoolEntry, edit_pool
        from indus.providers.auth.store import AuthStore
        store = AuthStore(Path(sys.argv[1]))
        tag = sys.argv[2]
        for i in range(15):
            edit_pool(store, "openrouter",
                      lambda pool, _d: pool.add(PoolEntry(id=f"{tag}{i:04d}", label=tag)))
        """
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(home), tag], cwd=REPO)
        for tag in ("a", "b")
    ]
    for proc in procs:
        assert proc.wait(timeout=60) == 0
    rows = json.loads((home / AUTH_FILE).read_text())["pool"]["openrouter"]
    assert len(rows) == 30
    assert sorted(r["priority"] for r in rows) == list(range(30))


def test_lock_is_reentrant_inside_a_transaction(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "h")
    with store.transaction() as doc:
        doc["active_provider"] = "x"
        with FileLock(store.path.with_name(AUTH_FILE + ".lock")):
            pass
    assert store.read()["active_provider"] == "x"


def test_guard_refuses_the_real_store_before_touching_disk() -> None:
    root = real_default_root()
    if root is None:
        pytest.skip("no passwd home on this platform")
    existed = root.exists()
    with pytest.raises(AuthIsolationRefused):
        AuthStore(root)
    with pytest.raises(AuthIsolationRefused):
        AuthStore(root / "profiles" / "work")
    assert root.exists() == existed


def test_unsafe_folders_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AuthUnsafePath):
        AuthStore(Path("/")).save(empty_document())
    inside = REPO / "src" / "indus"
    with pytest.raises(AuthUnsafePath):
        AuthStore(inside).save(empty_document())
    assert not (inside / AUTH_FILE).exists()


def test_named_profile_reads_the_root_store_read_only(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed(
        root,
        {
            "version": 1,
            "providers": {"openrouter": {"signed_in_at": "2026-01-01T00:00:00+00:00"}},
            "pool": {"openrouter": [{"id": "r1", "label": "root-key", "priority": 0}]},
        },
    )
    named = root / "profiles" / "work"
    store = AuthStore(named)
    state, inherited = store.provider_state("openrouter")
    assert inherited and state == {"signed_in_at": "2026-01-01T00:00:00+00:00"}
    rows, inherited_rows = store.pool_rows("openrouter")
    assert inherited_rows and rows[0]["label"] == "root-key"
    assert store.read() == empty_document()
    assert not (named / AUTH_FILE).exists()


def test_broken_root_store_reads_as_nothing_and_is_not_touched(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / AUTH_FILE).write_text("{oops")
    store = AuthStore(root / "profiles" / "work")
    assert store.pool_rows("openrouter") == ([], False)
    assert not (root / (AUTH_FILE + ".corrupt")).exists()


def test_signed_in_reads_records_oauth_rows_and_the_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed(
        root,
        {
            "version": 1,
            "providers": {"openrouter": {"signed_in_at": "2026-01-01T00:00:00+00:00"}, "x": {}},
            "pool": {"deepseek": [{"id": "k1", "auth_type": "api_key"}]},
        },
    )
    named = root / "profiles" / "work"
    _seed(named, {"version": 1, "pool": {"acme": [{"id": "o1", "auth_type": "oauth"}]}})
    assert AuthStore(root).signed_in() == ["openrouter"]
    assert AuthStore(named).signed_in() == ["acme", "openrouter"]
