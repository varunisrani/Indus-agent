"""The file-ops layer and path resolution (P05-15), over the host and a shell environment."""

from __future__ import annotations

import asyncio
import os
import random
import stat
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest

from indus.backends.contract import BackendFlags
from indus.foundation.scope import Scope
from indus.tools.builtin import file_ops, paths
from indus.tools.builtin.file_ops import (
    FileOpError,
    FileOps,
    HostFileOps,
    ShellFileOps,
    file_ops_for,
    ops_for_call,
)
from tests.fakes.agent import DictConfig
from tests.unit.tools.builtin._file_env import FakeRegistry, ShellEnv


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


@pytest.fixture(params=["host", "shell"])
def ops(request: pytest.FixtureRequest, tmp_path: Path) -> FileOps:
    if request.param == "host":
        return HostFileOps(str(tmp_path))
    return ShellFileOps(ShellEnv(str(tmp_path), noise="Welcome to the box\n"))


def test_three_megabyte_round_trip_is_byte_exact(ops: FileOps, tmp_path: Path) -> None:
    rng = random.Random(7)
    data = bytes(rng.randrange(256) for _ in range(3 * 1024 * 1024)) + b"\r\nlast line no newline"
    target = str(tmp_path / "big.bin")
    run(ops.write_bytes(target, data))
    assert Path(target).read_bytes() == data
    assert run(ops.read_bytes(target)) == data


def test_sentinel_shaped_text_inside_the_file_does_not_break_parsing(
    ops: FileOps, tmp_path: Path
) -> None:
    text = b"__INDUS_RF_0123456789abcdef0123456789abcdef__\n__INDUS_WF_ffff__\nfile\nmissing\n0\n"
    target = tmp_path / "tricky.txt"
    target.write_bytes(text)
    assert run(ops.read_bytes(str(target))) == text
    run(ops.write_bytes(str(target), text + b"more"))
    assert target.read_bytes() == text + b"more"


def test_missing_directory_and_stat(ops: FileOps, tmp_path: Path) -> None:
    with pytest.raises(FileOpError) as missing:
        run(ops.read_bytes(str(tmp_path / "nope.txt")))
    assert missing.value.kind == "missing"
    with pytest.raises(FileOpError) as directory:
        run(ops.read_bytes(str(tmp_path)))
    assert directory.value.kind == "is_dir"
    (tmp_path / "f.txt").write_bytes(b"12345")
    assert run(ops.stat(str(tmp_path / "f.txt"))) == file_ops.FileStat("file", 5)
    assert run(ops.stat(str(tmp_path))).kind == "dir"
    assert run(ops.stat(str(tmp_path / "nope"))).kind == "missing"
    import hashlib

    assert run(ops.sha256(str(tmp_path / "f.txt"))) == hashlib.sha256(b"12345").hexdigest()
    assert run(ops.sha256(str(tmp_path / "nope"))) is None
    assert "f.txt" in run(ops.list_dir(str(tmp_path)))


def test_fifo_is_not_read(ops: FileOps, tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(FileOpError) as caught:
        run(ops.read_bytes(str(fifo)))
    assert caught.value.kind == "not_regular"
    assert run(ops.stat(str(fifo))).kind == "other"


def test_write_creates_parents_and_keeps_mode_and_symlink(ops: FileOps, tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c.txt"
    assert run(ops.write_bytes(str(deep), b"x")) is True
    assert deep.read_bytes() == b"x"
    script = tmp_path / "run.sh"
    script.write_text("old")
    script.chmod(0o751)
    assert run(ops.write_bytes(str(script), b"new")) is False
    assert stat.S_IMODE(script.stat().st_mode) == 0o751
    real = tmp_path / "real.txt"
    real.write_text("before")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    run(ops.write_bytes(str(link), b"after"))
    assert link.is_symlink()
    assert real.read_bytes() == b"after"


def test_shell_calls_are_never_bounded(tmp_path: Path) -> None:
    env = ShellEnv(str(tmp_path))
    ops = ShellFileOps(env)
    run(ops.write_bytes(str(tmp_path / "x"), b"1"))
    run(ops.read_bytes(str(tmp_path / "x")))
    run(ops.stat(str(tmp_path / "x")))
    assert env.bounded and not any(env.bounded)


def test_noise_inside_the_fence_is_refused_as_garbled(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_bytes(b"hello")
    env = ShellEnv(str(tmp_path), inner_noise="TERM")
    with pytest.raises(FileOpError) as caught:
        run(ShellFileOps(env).read_bytes(str(tmp_path / "f.txt")))
    assert caught.value.kind == "garbled"


def test_od_transport_when_base64_is_missing(tmp_path: Path) -> None:
    data = bytes(range(256)) * 3
    (tmp_path / "f.bin").write_bytes(data)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "base64"
    fake.write_text("#!/bin/sh\nexit 127\n")
    fake.chmod(0o755)
    ops = ShellFileOps(ShellEnv(str(tmp_path), prefix=f"PATH={bin_dir}:$PATH; "))
    assert run(ops.read_bytes(str(tmp_path / "f.bin"))) == data


def test_file_ops_for_picks_host_or_shell_and_caches(tmp_path: Path) -> None:
    shell_env = ShellEnv(str(tmp_path))
    first = file_ops_for(shell_env)
    assert isinstance(first, ShellFileOps)
    assert file_ops_for(shell_env) is first
    file_ops.forget(shell_env)
    assert file_ops_for(shell_env) is not first

    class HostEnv(ShellEnv):
        @property
        def flags(self) -> BackendFlags:
            return BackendFlags(host_local=True)

    host_env = HostEnv(str(tmp_path))
    host_ops = file_ops_for(host_env)
    assert isinstance(host_ops, HostFileOps)
    host_env._cwd = "/elsewhere"
    assert host_ops.cwd == "/elsewhere"
    # Helper commands (shell lint checkers) run in the live environment behind either kind.
    assert file_ops.environment_of(host_ops) is host_env
    assert file_ops.environment_of(first) is shell_env
    assert file_ops.environment_of(HostFileOps(str(tmp_path))) is None


def test_ops_for_call_without_a_registry_uses_the_host(scope: Scope, tmp_path: Path) -> None:
    configured = scope.with_config(DictConfig({"terminal": {"cwd": str(tmp_path)}}))
    ops = run(ops_for_call(configured))
    assert isinstance(ops, HostFileOps)
    assert ops.cwd == str(tmp_path)


def test_ops_for_call_with_a_registry_acquires_a_non_local_env(
    scope: Scope, tmp_path: Path
) -> None:
    env = ShellEnv(str(tmp_path))
    registry = FakeRegistry(env)
    ops = run(ops_for_call(scope, session_id="s1", environments=registry))
    assert isinstance(ops, ShellFileOps)
    assert registry.acquired == 1
    local = FakeRegistry(env, backend="local")
    assert isinstance(run(ops_for_call(scope, environments=local)), HostFileOps)
    assert local.acquired == 0


# -- paths -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", ".", "./", "auto", "cwd", "AUTO", "relative/dir", None, 3])
def test_placeholder_and_relative_cwds_count_as_unset(value: object) -> None:
    assert paths.clean_cwd(value) is None


def test_dot_cwd_falls_back_to_the_configured_then_the_process_cwd(
    scope: Scope, tmp_path: Path
) -> None:
    configured = scope.with_config(DictConfig({"terminal": {"cwd": str(tmp_path)}}))
    assert paths.base_dir(configured, ".") == str(tmp_path)
    assert paths.base_dir(configured, "/srv/work") == "/srv/work"
    dotted = scope.with_config(DictConfig({"terminal": {"cwd": "."}}))
    assert paths.base_dir(dotted, "auto") == os.getcwd()


def test_tilde_uses_the_scope_home(scope: Scope, tmp_path: Path) -> None:
    home = tmp_path / "somebody"
    custom = scope.with_env(scope.env.with_overlay({"HOME": str(home)}))
    assert paths.expand_tilde("~/notes.md", custom) == f"{home}/notes.md"
    assert paths.expand_tilde("~", custom) == str(home)
    assert paths.expand_tilde("~other/x", custom) == "~other/x"
    assert paths.resolve("~/a/../b.txt", base="/x", scope=custom, host=False) == f"{home}/b.txt"
    assert paths.clean_cwd("~/proj", custom) == f"{home}/proj"


def test_relative_paths_anchor_on_the_base_and_host_paths_are_real(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real)
    assert paths.resolve("alias/f.txt", base=str(tmp_path), scope=None) == str(real / "f.txt")
    assert paths.resolve("alias/f.txt", base=str(tmp_path), scope=None, host=False) == str(
        tmp_path / "alias" / "f.txt"
    )
    assert paths.resolve("/abs/./x/../y", base="/ignored", scope=None, host=False) == "/abs/y"


def test_delete_removes_entries_not_link_targets(ops: FileOps, tmp_path: Path) -> None:
    (tmp_path / "gone.txt").write_text("x")
    run(ops.delete(str(tmp_path / "gone.txt")))
    assert not (tmp_path / "gone.txt").exists()
    (tmp_path / "keep.txt").write_text("k")
    (tmp_path / "link").symlink_to(tmp_path / "keep.txt")
    run(ops.delete(str(tmp_path / "link")))
    assert (tmp_path / "keep.txt").exists() and not (tmp_path / "link").is_symlink()
    with pytest.raises(FileOpError) as missing:
        run(ops.delete(str(tmp_path / "gone.txt")))
    assert missing.value.kind == "missing"
    with pytest.raises(FileOpError) as directory:
        run(ops.delete(str(tmp_path)))
    assert directory.value.kind == "is_dir"


def test_the_call_cancel_signal_reaches_the_shell(scope: Scope, tmp_path: Path) -> None:
    import threading

    env = ShellEnv(str(tmp_path))
    stop = threading.Event()
    ops = run(ops_for_call(scope, environments=FakeRegistry(env), cancel=stop))
    assert isinstance(ops, ShellFileOps) and ops.cancel is stop
    assert file_ops_for(env) is not ops  # a per-call object, the cache stays clean


def test_shell_protocol_through_the_real_local_backend(scope: Scope, tmp_path: Path) -> None:
    from indus.backends.local import open_local
    from indus.foundation.clock import SystemClock

    data = bytes(random.Random(3).randrange(256) for _ in range(300_000)) + b"\n__INDUS_RF_x__\n"

    async def scenario() -> tuple[bytes, object, bool]:
        env = await open_local(scope, cwd=str(tmp_path), clock=SystemClock())
        try:
            assert isinstance(file_ops_for(env), HostFileOps)
            shell = ShellFileOps(env)
            created = await shell.write_bytes("sub/data.bin", data)
            return await shell.read_bytes(str(tmp_path / "sub" / "data.bin")), env.cwd, created
        finally:
            await env.cleanup()

    read_back, cwd, created = run(scenario())
    assert created is True and read_back == data
    assert (tmp_path / "sub" / "data.bin").read_bytes() == data
    assert cwd == str(tmp_path)  # the protocol never moves the shell
