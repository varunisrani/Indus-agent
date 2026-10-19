"""Staged and streamed stdin for sandboxes (P21-03, R02.16)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from indus.backends.stdin_payload import (
    STAGE_PREFIX,
    STREAM_CHUNK,
    StagedStdin,
    StdinFailure,
    encode_stdin,
    stage_stdin,
    staged_stdin_path,
    stdin_chunks,
    stream_stdin,
    with_stdin,
)

MIB = 1024 * 1024
needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="runs the prefix in bash")


@dataclass
class FakeSandbox:
    """A pipe-less sandbox whose file API writes into a host dir and logs every step."""

    events: list[str] = field(default_factory=list[str])
    truncate_by: int = 0

    async def write(self, path: str, raw: bytes) -> None:
        self.events.append(f"write {len(raw)}")
        Path(path).write_bytes(raw[: len(raw) - self.truncate_by])

    async def size_of(self, path: str) -> int:
        self.events.append("size")
        return Path(path).stat().st_size

    async def remove(self, path: str) -> None:
        self.events.append("remove")
        Path(path).unlink()

    def exec(self, script: str, cwd: Path) -> subprocess.CompletedProcess[bytes]:
        self.events.append("exec")
        return subprocess.run(  # noqa: S603 - the test's own script
            ["bash", "-c", script], cwd=cwd, capture_output=True, timeout=30, check=False
        )


def _stage(sandbox: FakeSandbox, data: str | None, temp: Path) -> StagedStdin:
    temp.mkdir(exist_ok=True)
    return asyncio.run(
        stage_stdin(
            data,
            temp_dir=str(temp),
            write=sandbox.write,
            size_of=sandbox.size_of,
            remove=sandbox.remove,
        )
    )


@needs_bash
def test_one_mib_payload_staged_verified_and_removed_before_the_command(tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    payload = ("0123456789abcdef" * (MIB // 16 - 1)) + "é end"
    staged = _stage(sandbox, payload, tmp_path)
    assert staged.error is None and staged.path is not None
    assert staged.size == len(payload.encode("utf-8"))
    assert Path(staged.path).name.startswith(STAGE_PREFIX)
    script = with_stdin(
        f"if [ -e {staged.path} ]; then echo present; else echo gone; fi; wc -c", staged
    )
    done = sandbox.exec(script, tmp_path)
    assert done.returncode == 0
    assert done.stdout.split() == [b"gone", str(staged.size).encode()]
    assert sandbox.events == [f"write {staged.size}", "size", "exec"]
    assert not Path(staged.path).exists()


@needs_bash
def test_prefix_removes_file_before_command(tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    temp = tmp_path / "sandbox-tmp"
    staged = _stage(sandbox, "line one\nline two", temp)
    done = sandbox.exec(with_stdin("ls -A; cat", staged), temp)
    assert done.stdout == b"line one\nline two"
    assert list(temp.iterdir()) == []


@needs_bash
def test_missing_staged_file_stops_the_script(tmp_path: Path) -> None:
    gone = StagedStdin(path=str(tmp_path / "never-uploaded"), size=1)
    done = FakeSandbox().exec(with_stdin("echo ran", gone), tmp_path)
    assert done.returncode != 0 and b"ran" not in done.stdout


def test_size_mismatch_is_a_stdin_error_and_the_file_is_removed(tmp_path: Path) -> None:
    sandbox = FakeSandbox(truncate_by=3)
    temp = tmp_path / "sandbox-tmp"
    staged = _stage(sandbox, "abcdefgh", temp)
    assert staged.path is None and staged.prefix() == ""
    assert staged.error == "stdin upload was incomplete (5 of 8 bytes arrived)"
    assert sandbox.events == ["write 8", "size", "remove"]
    assert list(temp.iterdir()) == []


@pytest.mark.parametrize("data", [None, ""])
def test_empty_stdin_stages_nothing(tmp_path: Path, data: str | None) -> None:
    sandbox = FakeSandbox()
    staged = _stage(sandbox, data, tmp_path)
    assert staged == StagedStdin() and sandbox.events == []
    assert with_stdin("cat", staged) == "cat"


def test_non_utf8_payload_is_a_stdin_error(tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    staged = _stage(sandbox, "bad \ud800 surrogate", tmp_path)
    assert staged.path is None and staged.error is not None
    assert staged.error.startswith("stdin is not valid text")
    assert sandbox.events == []
    assert isinstance(encode_stdin("\ud800"), StdinFailure)
    assert encode_stdin("raw \udcff byte") == b"raw \xff byte"  # surrogateescape round trip


def test_write_failure_propagates(tmp_path: Path) -> None:
    async def broken(path: str, raw: bytes) -> None:
        raise ConnectionResetError("vendor dropped the upload")

    async def size(path: str) -> int:
        return 0

    with pytest.raises(ConnectionResetError):
        asyncio.run(stage_stdin("x", temp_dir=str(tmp_path), write=broken, size_of=size))


def test_staged_path_is_unique_and_quoted() -> None:
    first, second = staged_stdin_path("/tmp/"), staged_stdin_path("/tmp")  # noqa: S108 - text only
    assert first != second and first.startswith("/tmp/.indus-stdin-")  # noqa: S108
    assert staged_stdin_path("/").startswith("/.indus-stdin-")
    odd = StagedStdin(path="/t m p/it's", size=1)
    quoted = "'/t m p/it'\"'\"'s'"
    assert odd.prefix() == f"exec 0<{quoted} || exit 1; rm -f {quoted}\n"


@dataclass
class RecordingWriter:
    calls: list[tuple[str, int]] = field(default_factory=list[tuple[str, int]])

    def write(self, data: bytes, /) -> None:
        self.calls.append(("write", len(data)))

    def drain(self) -> None:
        self.calls.append(("drain", 0))

    def write_eof(self) -> None:
        self.calls.append(("eof", 0))


def test_chunks_at_64k_boundaries() -> None:
    raw = b"x" * (2 * STREAM_CHUNK + 10)
    assert [len(c) for c in stdin_chunks(raw)] == [STREAM_CHUNK, STREAM_CHUNK, 10]
    assert [len(c) for c in stdin_chunks(b"y" * (3 * STREAM_CHUNK))] == [STREAM_CHUNK] * 3
    writer = RecordingWriter()
    stream_stdin(writer, raw)
    assert writer.calls == [
        ("write", STREAM_CHUNK),
        ("drain", 0),
        ("write", STREAM_CHUNK),
        ("drain", 0),
        ("write", 10),
        ("drain", 0),
        ("eof", 0),
        ("drain", 0),
    ]
    with pytest.raises(ValueError):
        list(stdin_chunks(b"x", 0))


def test_empty_stream_sends_only_eof() -> None:
    writer = RecordingWriter()
    stream_stdin(writer, b"")
    assert writer.calls == [("eof", 0), ("drain", 0)]


@pytest.mark.skipif(shutil.which("bash") is None, reason="the fake modal runs bash")
def test_stream_into_the_fake_modal_process(tmp_path: Path) -> None:
    from tests.fakes.backend.fake_modal import FakeModal
    from tests.fakes.backend.sdk_kit import Cloud

    cloud = Cloud(tmp_path / "cloud")
    modal = FakeModal(cloud).module
    sandbox = modal.Sandbox.create(app=modal.App.lookup("indus", create_if_missing=True))
    process = sandbox.exec("bash", "-c", "wc -c")
    raw = encode_stdin("z" * (MIB + 1))
    assert isinstance(raw, bytes)
    stream_stdin(process.stdin, raw)
    assert process.stdout.read().strip() == str(MIB + 1)
    writes = [c for c in cloud.calls if c.method == "StreamWriter.write"]
    assert len(writes) == MIB // STREAM_CHUNK + 1
