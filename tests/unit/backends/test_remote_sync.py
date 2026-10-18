"""Remote file sync, push side (P11-10): fake transport and fake clock, plus the SSH transport
against the fake ssh CLI."""

from __future__ import annotations

import hashlib
import os
import tarfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from indus.backends.contract import BackendError
from indus.backends.remote_sync import (
    FORCE_SYNC_ENV,
    SYNC_INTERVAL_S,
    FileSync,
    SshTransport,
    SyncItem,
    write_archive,
)
from indus.backends.ssh.client import SshTarget
from indus.backends.ssh.runner import run_client
from tests.fakes.backend.ssh_cli import install
from tests.fakes.clock import FakeClock
from tests.unit.backends._aio import aio

BASE = "/remote/home/.indus"


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.remote: dict[str, bytes] = {}
        self.fail = False

    def _check(self) -> None:
        if self.fail:
            raise BackendError("the link dropped")

    async def upload_one(self, path: str, dest: str) -> None:
        self._check()
        self.calls.append(("one", dest))
        self.remote[dest] = Path(path).read_bytes()

    async def upload_bulk(self, tar_path: str) -> None:
        self._check()
        with tarfile.open(tar_path) as archive:
            names = sorted(archive.getnames())
            for member in archive.getmembers():
                handle = archive.extractfile(member)
                assert handle is not None
                self.remote[f"{BASE}/{member.name}"] = handle.read()
        self.calls.append(("bulk", names))

    async def download_bulk(self, src: str, dest: str) -> None:
        raise AssertionError("SSH never syncs back")

    async def delete(self, paths: Sequence[str]) -> None:
        self._check()
        self.calls.append(("delete", list(paths)))
        for path in paths:
            self.remote.pop(path, None)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 18, 12, 0, tzinfo=UTC))


def _age(path: Path, clock: FakeClock, seconds_ago: float, *, ns: int | None = None) -> None:
    when = ns if ns is not None else int((clock.now().timestamp() - seconds_ago) * 1e9)
    os.utime(path, ns=(when, when))


class Tree:
    def __init__(self, root: Path, clock: FakeClock) -> None:
        self.root = root
        self.clock = clock
        root.mkdir(parents=True, exist_ok=True)

    def put(self, rel: str, text: str, *, seconds_ago: float = 600.0) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        _age(path, self.clock, seconds_ago)
        return path

    def items(self) -> list[SyncItem]:
        return [
            SyncItem(str(p), f"{BASE}/skills/{p.relative_to(self.root).as_posix()}")
            for p in sorted(self.root.rglob("*"))
            if p.is_file()
        ]


def make_sync(tree: Tree, transport: FakeTransport, clock: FakeClock, **kw: object) -> FileSync:
    return FileSync(tree.items, transport, base=BASE, clock=clock, **kw)  # type: ignore[arg-type]


@aio
async def test_first_call_pushes_all_then_rate_limited(tmp_path: Path, clock: FakeClock) -> None:
    tree = Tree(tmp_path / "skills", clock)
    tree.put("a/SKILL.md", "alpha")
    tree.put("b/SKILL.md", "beta")
    transport = FakeTransport()
    sync = make_sync(tree, transport, clock)
    report = await sync.sync()
    assert report is not None and len(report.uploaded) == 2
    assert transport.calls == [("bulk", ["skills/a/SKILL.md", "skills/b/SKILL.md"])]
    assert sync.pushed[f"{BASE}/skills/a/SKILL.md"] == hashlib.sha256(b"alpha").hexdigest()
    clock.advance(SYNC_INTERVAL_S - 1)
    assert await sync.sync() is None  # within 5 s: not even a scan
    clock.advance(1)
    report = await sync.sync()
    assert report is not None and not report.uploaded and not report.deleted
    assert len(transport.calls) == 1


@aio
async def test_racy_mtime_is_hashed_and_pushed_when_bytes_differ(
    tmp_path: Path, clock: FakeClock
) -> None:
    tree = Tree(tmp_path / "skills", clock)
    path = tree.put("hot.md", "v1-aaaa", seconds_ago=1.0)  # inside the 2 s window
    stamp = os.stat(path).st_mtime_ns
    transport = FakeTransport()
    sync = make_sync(tree, transport, clock)
    await sync.sync()
    assert transport.calls == [("one", f"{BASE}/skills/hot.md")]
    # Rewritten in the same timestamp tick with the same size: stat cannot tell.
    path.write_text("v2-bbbb")
    _age(path, clock, 0, ns=stamp)
    clock.advance(SYNC_INTERVAL_S)
    await sync.sync()
    assert transport.calls[-1] == ("one", f"{BASE}/skills/hot.md")
    assert transport.remote[f"{BASE}/skills/hot.md"] == b"v2-bbbb"
    clock.advance(SYNC_INTERVAL_S)
    await sync.sync()
    assert len(transport.calls) == 2  # now old enough to trust the stat key


@aio
async def test_racy_file_with_same_bytes_is_not_pushed_again(
    tmp_path: Path, clock: FakeClock
) -> None:
    tree = Tree(tmp_path / "skills", clock)
    tree.put("hot.md", "same", seconds_ago=0.5)
    transport = FakeTransport()
    sync = make_sync(tree, transport, clock)
    await sync.sync()
    clock.advance(SYNC_INTERVAL_S)
    report = await sync.sync()
    assert report is not None and not report.uploaded
    assert len(transport.calls) == 1


@aio
async def test_deleted_file_is_deleted_remotely(tmp_path: Path, clock: FakeClock) -> None:
    tree = Tree(tmp_path / "skills", clock)
    tree.put("keep.md", "k")
    gone = tree.put("gone.md", "g")
    transport = FakeTransport()
    sync = make_sync(tree, transport, clock)
    await sync.sync()
    gone.unlink()
    clock.advance(SYNC_INTERVAL_S)
    await sync.sync()
    assert transport.calls[-1] == ("delete", [f"{BASE}/skills/gone.md"])
    assert (
        f"{BASE}/skills/gone.md" not in sync.pushed and f"{BASE}/skills/gone.md" not in sync.synced
    )


@aio
async def test_transport_failure_keeps_state_and_retries(tmp_path: Path, clock: FakeClock) -> None:
    tree = Tree(tmp_path / "skills", clock)
    tree.put("a.md", "a")
    tree.put("b.md", "b")
    transport = FakeTransport()
    transport.fail = True
    sync = make_sync(tree, transport, clock)
    assert await sync.sync() is None
    assert sync.synced == {} and sync.pushed == {}
    transport.fail = False
    report = await sync.sync()  # retried by the next command, no 5 s wait after a failure
    assert report is not None and len(report.uploaded) == 2
    # A failed delete leaves the file recorded so it is deleted next time.
    (tmp_path / "skills" / "a.md").unlink()
    clock.advance(SYNC_INTERVAL_S)
    transport.fail = True
    await sync.sync()
    assert f"{BASE}/skills/a.md" in sync.synced
    transport.fail = False
    await sync.sync()
    assert transport.calls[-1] == ("delete", [f"{BASE}/skills/a.md"])


@aio
async def test_force_flag_skips_the_gate(tmp_path: Path, clock: FakeClock) -> None:
    tree = Tree(tmp_path / "skills", clock)
    tree.put("a.md", "a")
    transport = FakeTransport()
    sync = make_sync(tree, transport, clock, env={FORCE_SYNC_ENV: "1"})
    await sync.sync()
    tree.put("b.md", "b")
    assert await sync.sync() is not None  # no time passed
    assert transport.calls[-1] == ("one", f"{BASE}/skills/b.md")


def test_archive_refuses_paths_outside_the_base(tmp_path: Path) -> None:
    source = tmp_path / "f"
    source.write_text("x")
    with pytest.raises(BackendError):
        write_archive([SyncItem(str(source), "/elsewhere/f")], BASE, str(tmp_path / "a.tar"))
    with pytest.raises(BackendError):
        write_archive([SyncItem(str(source), f"{BASE}/../f")], BASE, str(tmp_path / "b.tar"))


def test_archive_hashes_exactly_what_it_packs(tmp_path: Path) -> None:
    source = tmp_path / "cred"
    source.write_bytes(b"\x00\x01binary")
    out = tmp_path / "a.tar"
    hashes = write_archive(
        [SyncItem(str(source), f"{BASE}/credentials/cred", mode=0o600)], BASE, str(out)
    )
    assert hashes == {f"{BASE}/credentials/cred": hashlib.sha256(b"\x00\x01binary").hexdigest()}
    with tarfile.open(out) as archive:
        member = archive.getmember("credentials/cred")
        assert member.mode == 0o600 and member.size == 8
        assert not [m for m in archive.getmembers() if m.isdir()]  # no dir entries (modes kept)


@aio
async def test_ssh_transport_moves_files(tmp_path: Path) -> None:
    fake = install(tmp_path / "bin", tmp_path / "remote-home")
    base = str(fake.remote_home / ".indus")
    env = {"PATH": fake.path_env(os.environ.get("PATH", ""))}
    transport = SshTransport(
        ssh=str(fake.bin_dir / "ssh"),
        scp=str(fake.bin_dir / "scp"),
        target=SshTarget("box.test", "dev"),
        control=None,
        env=env,
        runner=run_client,
        base=base,
    )
    one = tmp_path / "one.txt"
    one.write_text("single")
    await transport.upload_one(str(one), f"{base}/cache/images/one.txt")
    assert (fake.remote_home / ".indus/cache/images/one.txt").read_text() == "single"
    many = [tmp_path / "m1", tmp_path / "m2"]
    for index, path in enumerate(many):
        path.write_text(f"m{index}")
    tar_path = str(tmp_path / "bulk.tar")
    write_archive([SyncItem(str(p), f"{base}/skills/x/{p.name}") for p in many], base, tar_path)
    await transport.upload_bulk(tar_path)
    assert (fake.remote_home / ".indus/skills/x/m2").read_text() == "m1"
    await transport.delete([f"{base}/skills/x/m1", f"{base}/cache/images/one.txt"])
    assert not (fake.remote_home / ".indus/skills/x/m1").exists()
    assert (fake.remote_home / ".indus/skills/x/m2").exists()
    tools = [c["tool"] for c in fake.calls()]
    assert tools.count("scp") == 1
