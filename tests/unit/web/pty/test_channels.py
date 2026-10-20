"""P18-08: channel ids, per-channel active-session files and the fresh-chat working directory."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from indus.web.pty.channels import ChannelFiles, CwdRefused, resolve_cwd, valid_channel


@pytest.mark.parametrize("value", ["a", "chat-1.x_Y", "A" * 128, "0"])
def test_valid_channel_ids(value: str) -> None:
    assert valid_channel(value) == value


@pytest.mark.parametrize("value", ["", "A" * 129, "a b", "../x", "a/b", "é", "x\n", None, 5])
def test_invalid_channel_ids(value: object) -> None:
    assert valid_channel(value) is None


def test_claim_read_clear_release(tmp_path: Path) -> None:
    files = ChannelFiles(tmp_path / "chat")
    path, existed = files.claim("c1")
    assert not existed and path.is_file()
    assert files.read("c1") is None  # empty until the TUI writes
    path.write_text('{"session_id": "20261020_101010_abcd"}', encoding="utf-8")
    assert files.read("c1") == "20261020_101010_abcd"
    again, existed = files.claim("c1")
    assert again == path and existed
    files.clear("c1")
    assert not path.exists() and files.read("c1") is None
    fresh, existed = files.claim("c1")
    assert not existed and fresh != path
    files.release("c1", path)  # not the channel's file any more: the channel keeps its own
    assert files.path("c1") == fresh
    files.release("c1", fresh)
    assert not files.known("c1") and not fresh.exists()
    with pytest.raises(ValueError):
        files.claim("bad channel")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_files_are_owner_only(tmp_path: Path) -> None:
    files = ChannelFiles(tmp_path / "chat")
    path, _ = files.claim("c1")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(files.directory).st_mode) == 0o700
    own = ChannelFiles()
    try:
        assert stat.S_IMODE(os.stat(own.directory).st_mode) == 0o700
    finally:
        own.close()
    assert not own.directory.exists()


@pytest.mark.parametrize(
    "content",
    [b"", b"not json", b"[1, 2]", b'{"session_id": 5}', b'{"session_id": "  "}', b"x" * 5000],
)
def test_a_bad_file_reads_as_no_session(tmp_path: Path, content: bytes) -> None:
    files = ChannelFiles(tmp_path / "chat")
    path, _ = files.claim("c1")
    path.write_bytes(content)
    assert files.read("c1") is None


def test_close_removes_the_files(tmp_path: Path) -> None:
    files = ChannelFiles(tmp_path / "chat")
    a, _ = files.claim("a")
    b, _ = files.claim("b")
    files.close()
    assert not a.exists() and not b.exists()
    assert not files.known("a")


def test_cwd_rules(tmp_path: Path) -> None:
    assert resolve_cwd(None) is None
    assert resolve_cwd("  ") is None
    assert resolve_cwd(str(tmp_path)) == tmp_path.resolve()
    with pytest.raises(CwdRefused, match="absolute"):
        resolve_cwd("relative/dir")
    with pytest.raises(CwdRefused, match="does not exist"):
        resolve_cwd(str(tmp_path / "missing"))
    afile = tmp_path / "file.txt"
    afile.write_text("x", encoding="utf-8")
    with pytest.raises(CwdRefused, match="not a directory"):
        resolve_cwd(str(afile))
    with pytest.raises(CwdRefused):
        resolve_cwd(str(tmp_path) + "\x00x")
    with pytest.raises(CwdRefused):
        resolve_cwd(42)
