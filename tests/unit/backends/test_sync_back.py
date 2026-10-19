"""Sync-back from SDK sandboxes (P21-10, R02.31): archives are built by the tests with tarfile."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import signal
import tarfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from indus.backends.remote_sync import FileSync, SyncItem
from indus.backends.sync_back import (
    SyncBackReport,
    defer_sigint,
    sweep_stale,
    sync_back,
    temp_root,
)
from indus.foundation.clock import SystemClock
from indus.foundation.fsio import FileLock
from tests.fakes.clock import FakeClock
from tests.unit.backends._aio import aio
from tests.unit.backends.conftest import drive

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX file semantics")

BASE = "/root/.indus"
ROOTS = [(f"{BASE}/skills", "skills"), (f"{BASE}/cache/documents", "cache/documents")]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TarTransport:
    """A transport whose download hands out a prepared archive (after ``failures`` errors)."""

    def __init__(self, archive: bytes, failures: int = 0) -> None:
        self.archive = archive
        self.failures = failures
        self.downloads: list[str] = []

    async def upload_one(self, path: str, dest: str) -> None:
        raise AssertionError("not used")

    async def upload_bulk(self, tar_path: str) -> None:
        raise AssertionError("not used")

    async def download_bulk(self, src: str, dest: str) -> None:
        self.downloads.append(src)
        if self.failures:
            self.failures -= 1
            raise OSError("link dropped")
        Path(dest).write_bytes(self.archive)

    async def delete(self, paths: Sequence[str]) -> None:
        raise AssertionError("not used")


def build_tar(entries: list[tuple[str, bytes | None, str]]) -> bytes:
    """(name, data, kind) with kind file | symlink:<target> | dir | hardlink:<target>."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                assert data is not None
                info.size = len(data)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif kind.startswith("symlink:"):
                info.type = tarfile.SYMTYPE
                info.linkname = kind.split(":", 1)[1]
                archive.addfile(info)
            elif kind.startswith("hardlink:"):
                info.type = tarfile.LNKTYPE
                info.linkname = kind.split(":", 1)[1]
                archive.addfile(info)
    return buffer.getvalue()


def make_sync(transport: TarTransport, pushed: dict[str, str], upload_only=()) -> FileSync:
    sync = FileSync(lambda: list[SyncItem](), transport, base=BASE, clock=SystemClock())
    sync.pushed = dict(pushed)
    sync.synced = {path: (1, 1) for path in pushed}
    sync.upload_only = set(upload_only)
    return sync


def roots(home: Path) -> list[tuple[str, str]]:
    return [(remote, str(home / host)) for remote, host in ROOTS]


async def run(
    sync: FileSync, home: Path, *, max_bytes: int = 1 << 31, clock=None, on_write=None, **kw
) -> SyncBackReport:
    return await sync_back(
        sync,
        home=home,
        remote_dir=BASE,
        roots=roots(home),
        max_bytes=max_bytes,
        clock=clock or SystemClock(),
        on_write=on_write,
        **kw,
    )


@aio
async def test_only_the_changed_and_new_files_are_written(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    (home / "skills" / "a.md").write_bytes(b"old a")
    (home / "skills" / "b.md").write_bytes(b"same b")
    secret = tmp_path / "token.json"
    secret.write_bytes(b"secret v1")
    archive = build_tar(
        [
            ("root/.indus/skills", None, "dir"),
            ("root/.indus/skills/a.md", b"new a", "file"),
            ("root/.indus/skills/b.md", b"same b", "file"),
            ("root/.indus/skills/new.md", b"brand new", "file"),
            ("root/.indus/credentials/token.json", b"secret v2", "file"),
            ("root/.indus/skills/../../../../escape.txt", b"x", "file"),
            ("/etc/absolute.txt", b"x", "file"),
            ("root/.indus/skills/link.md", None, "symlink:/etc/passwd"),
            ("root/.indus/skills/hard.md", None, "hardlink:root/.indus/skills/a.md"),
            ("root/.indus/other/outside.md", b"x", "file"),
        ]
    )
    sync = make_sync(
        TarTransport(archive),
        {
            f"{BASE}/skills/a.md": sha(b"old a"),
            f"{BASE}/skills/b.md": sha(b"same b"),
            f"{BASE}/credentials/token.json": sha(b"secret v1"),
        },
        upload_only={f"{BASE}/credentials/token.json"},
    )
    b_mtime = (home / "skills" / "b.md").stat().st_mtime_ns
    report = await run(sync, home, upload_only_hosts=[str(secret)])
    assert report.status == "applied"
    assert sorted(Path(p).name for p in report.written) == ["a.md", "new.md"]
    assert (home / "skills" / "a.md").read_bytes() == b"new a"
    assert (home / "skills" / "new.md").read_bytes() == b"brand new"
    assert (home / "skills" / "b.md").stat().st_mtime_ns == b_mtime  # not rewritten
    assert secret.read_bytes() == b"secret v1"
    assert not (tmp_path / "escape.txt").exists() and not (home / "escape.txt").exists()
    assert not (home / "skills" / "link.md").exists()
    assert not (home / "skills" / "hard.md").exists()
    reasons = dict(report.refused)
    assert reasons["root/.indus/credentials/token.json"].startswith("credential")
    assert "leaves" in reasons["root/.indus/skills/../../../../escape.txt"]
    assert reasons["/etc/absolute.txt"] == "absolute path"
    assert reasons["root/.indus/skills/link.md"] == "link"
    assert reasons["root/.indus/skills/hard.md"] == "link"
    assert reasons["root/.indus/other/outside.md"] == "outside the mirrored dirs"


@aio
async def test_credential_is_never_written_even_inside_a_mirrored_dir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    key = home / "skills" / "key.pem"
    key.write_bytes(b"k1")
    archive = build_tar([("root/.indus/skills/key.pem", b"k2", "file")])
    sync = make_sync(TarTransport(archive), {f"{BASE}/skills/other.md": sha(b"x")})
    report = await run(sync, home, upload_only_hosts=[str(key)])
    assert report.written == [] and key.read_bytes() == b"k1"


@aio
async def test_symlinked_host_dir_cannot_redirect_a_write(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "skills" / "sub").symlink_to(outside, target_is_directory=True)
    (home / "skills" / "top.md").symlink_to(outside / "top.md")
    archive = build_tar(
        [
            ("root/.indus/skills/sub/x.md", b"x", "file"),
            ("root/.indus/skills/top.md", b"y", "file"),
        ]
    )
    sync = make_sync(TarTransport(archive), {f"{BASE}/skills/a.md": sha(b"a")})
    report = await run(sync, home)
    assert report.written == []
    assert list(outside.iterdir()) == []
    reasons = dict(report.refused)
    assert "symlink" in reasons["root/.indus/skills/sub/x.md"]
    assert "symlink" in reasons["root/.indus/skills/top.md"]


@aio
async def test_archive_over_the_limit_is_refused_whole(tmp_path: Path) -> None:
    home = tmp_path / "home"
    archive = build_tar([("root/.indus/skills/a.md", b"new", "file")])
    pushed = {f"{BASE}/skills/a.md": sha(b"old")}
    refused = await run(make_sync(TarTransport(archive), pushed), home, max_bytes=len(archive) - 1)
    assert refused.status == "refused" and not (home / "skills" / "a.md").exists()
    taken = await run(make_sync(TarTransport(archive), pushed), home, max_bytes=len(archive))
    assert taken.status == "applied" and (home / "skills" / "a.md").read_bytes() == b"new"


@aio
async def test_nothing_pushed_means_no_download(tmp_path: Path) -> None:
    transport = TarTransport(build_tar([("root/.indus/skills/a.md", b"x", "file")]))
    sync = make_sync(transport, {})
    report = await run(sync, tmp_path / "home")
    assert report.status == "skipped" and transport.downloads == []
    assert not (tmp_path / "home" / "skills").exists()


@aio
async def test_unchanged_archive_writes_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    (home / "skills" / "a.md").write_bytes(b"a")
    before = (home / "skills" / "a.md").stat().st_mtime_ns
    archive = build_tar([("root/.indus/skills/a.md", b"a", "file")])
    report = await run(make_sync(TarTransport(archive), {f"{BASE}/skills/a.md": sha(b"a")}), home)
    assert report.status == "applied" and report.written == [] and report.refused == []
    assert (home / "skills" / "a.md").stat().st_mtime_ns == before


@aio
async def test_failed_downloads_retry_after_2_then_4_seconds(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    clock = FakeClock(datetime(2026, 10, 19, tzinfo=UTC))
    slept: list[float] = []
    real_sleep = clock.sleep

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        await real_sleep(seconds)

    clock.sleep = sleep  # type: ignore[method-assign]
    home = tmp_path / "home"
    transport = TarTransport(build_tar([("root/.indus/skills/a.md", b"new", "file")]), failures=2)
    sync = make_sync(transport, {f"{BASE}/skills/a.md": sha(b"old")})
    report = await drive(clock, run(sync, home, clock=clock))
    assert report.status == "applied" and slept == [2.0, 4.0]
    assert len(transport.downloads) == 3

    transport = TarTransport(b"", failures=5)
    report = await drive(clock, run(make_sync(transport, {"x": "y"}), home, clock=clock))
    assert report.status == "failed" and len(transport.downloads) == 3


@aio
async def test_the_apply_waits_for_the_profile_lock(tmp_path: Path) -> None:
    home = tmp_path / "home"
    archive = build_tar([("root/.indus/skills/a.md", b"new", "file")])
    held = FileLock(temp_root(home).parent / "sync-back.lock")
    temp_root(home).mkdir(parents=True)
    held.acquire()  # another process (or sandbox) is applying right now
    try:
        task = asyncio.ensure_future(run(make_sync(TarTransport(archive), {"x": "y"}), home))
        done, _ = await asyncio.wait({task}, timeout=0.5)
        assert not done and not (home / "skills" / "a.md").exists()
    finally:
        held.release()
    report = await task
    assert report.status == "applied" and (home / "skills" / "a.md").read_bytes() == b"new"


@aio
async def test_two_sync_backs_both_apply(tmp_path: Path) -> None:
    home = tmp_path / "home"

    def archive(tag: str) -> bytes:
        return build_tar(
            [(f"root/.indus/skills/{tag}{i}.md", f"{tag}{i}".encode(), "file") for i in range(2)]
        )

    first = run(make_sync(TarTransport(archive("a")), {"x": "y"}), home)
    second = run(make_sync(TarTransport(archive("b")), {"x": "y"}), home)
    reports = await asyncio.gather(first, second)
    assert all(r.status == "applied" and len(r.written) == 2 for r in reports)
    assert len(list((home / "skills").iterdir())) == 4


def test_ctrl_c_during_apply_finishes_the_apply_then_raises(tmp_path: Path) -> None:
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signals need the main thread")
    home = tmp_path / "home"
    sent: list[str] = []

    def on_write(path: str) -> None:
        if not sent:
            sent.append(path)
            os.kill(os.getpid(), signal.SIGINT)

    archive = build_tar(
        [(f"root/.indus/skills/f{i}.md", f"v{i}".encode(), "file") for i in range(3)]
    )
    sync = make_sync(TarTransport(archive), {"x": "y"})
    finished: list[bool] = []

    async def main() -> None:
        await run(sync, home, on_write=on_write)
        finished.append(True)
        later = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_later(5, later.set_result, None)
        await later  # the held Ctrl+C cancels this

    # A runner started in the background may ignore SIGINT; this test needs the default.
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(main())
    finally:
        signal.signal(signal.SIGINT, previous)
    assert finished == [True]  # the sync-back returned normally first
    assert sorted(p.name for p in (home / "skills").iterdir()) == ["f0.md", "f1.md", "f2.md"]


def test_defer_sigint_off_the_main_thread_is_a_no_op() -> None:
    done: list[bool] = []

    def work() -> None:
        with defer_sigint():
            done.append(True)

    worker = threading.Thread(target=work)
    worker.start()
    worker.join()
    assert done == [True]


def test_stale_temp_of_a_dead_owner_is_removed_live_kept(tmp_path: Path) -> None:
    root = tmp_path / "sync-back"
    root.mkdir()
    old = time.time() - 31 * 60
    dead = root / "4242-aaaa"
    live = root / f"{os.getpid()}-bbbb"
    young_dead = root / "4343-cccc"
    other = root / "notes"
    for entry in (dead, live, young_dead, other):
        entry.mkdir()
        (entry / "remote.tar").write_bytes(b"x")
    for entry in (dead, live, other):
        os.utime(entry, (old, old))
    removed = sweep_stale(root, pid_alive=lambda pid: pid == os.getpid())
    assert removed == 1
    assert not dead.exists() and live.exists() and young_dead.exists() and other.exists()
