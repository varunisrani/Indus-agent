"""``read_file``: guards, paging, budget, loop breaker and baselines (P05-16)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.tools.builtin import read_tracking
from indus.tools.builtin.read_file import READ_FILE_SCHEMA, read_file, register_tools
from indus.tools.calls import ToolResult
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import DictConfig
from tests.unit.tools.builtin._file_env import FakeRegistry, ShellEnv


@pytest.fixture(autouse=True)
def fresh_trackers() -> Iterator[None]:
    read_tracking.reset_all()
    yield
    read_tracking.reset_all()


def call(scope: Scope | None, path: object, task: str = "t1", **args: Any) -> dict[str, Any]:
    text = asyncio.run(read_file({"path": path, **args}, scope=scope, task_id=task))
    return json.loads(text)


def raw_call(scope: Scope | None, path: str, **kwargs: Any) -> str:
    return asyncio.run(read_file({"path": path}, scope=scope, task_id="t1", **kwargs))


def lines_file(tmp_path: Path, count: int, name: str = "f.txt") -> Path:
    target = tmp_path / name
    target.write_text("".join(f"line {i}\n" for i in range(1, count + 1)))
    return target


def baseline(scope: Scope, path: Path) -> str | None:
    return read_tracking.tracker_for(scope, "t1").baseline(os.path.realpath(path))


# -- paging ----------------------------------------------------------------------------------------


def test_numbered_lines_and_paging(scope: Scope, tmp_path: Path) -> None:
    target = lines_file(tmp_path, 10)
    first = call(scope, str(target), limit=4)
    assert first["content"] == "1|line 1\n2|line 2\n3|line 3\n4|line 4"
    assert first["total_lines"] == 10
    assert first["truncated"] is True
    assert first["next_offset"] == 5
    assert first["file_size"] == target.stat().st_size
    last = call(scope, str(target), offset=9, limit=4)
    assert last["content"] == "9|line 9\n10|line 10"
    assert last["truncated"] is False
    assert "next_offset" not in last


def test_offset_and_limit_are_coerced(scope: Scope, tmp_path: Path) -> None:
    target = lines_file(tmp_path, 3)
    body = call(scope, str(target), offset="2", limit="0")
    assert body["content"] == "2|line 2\n3|line 3"
    body = call(scope, str(target), task="t2", offset=-4, limit=99999)
    assert body["content"].startswith("1|line 1")


def test_offset_past_the_end(scope: Scope, tmp_path: Path) -> None:
    target = lines_file(tmp_path, 3)
    assert "past the end" in call(scope, str(target), offset=7)["error"]


def test_relative_path_anchors_on_the_configured_cwd(scope: Scope, tmp_path: Path) -> None:
    lines_file(tmp_path, 2, "rel.txt")
    configured = scope.with_config(DictConfig({"terminal": {"cwd": str(tmp_path)}}))
    assert call(configured, "rel.txt")["total_lines"] == 2


def test_empty_file(scope: Scope, tmp_path: Path) -> None:
    (tmp_path / "e.txt").write_text("")
    body = call(scope, str(tmp_path / "e.txt"))
    assert body["content"] == "" and body["total_lines"] == 0 and body["truncated"] is False


def test_char_budget_cuts_on_a_line_boundary(scope: Scope, tmp_path: Path) -> None:
    target = lines_file(tmp_path, 50)
    small = scope.with_config(DictConfig({"file_read_max_chars": 40}))
    body = call(small, str(target))
    assert body["content"] == "1|line 1\n2|line 2\n3|line 3\n4|line 4"
    assert body["truncated_by"] == "chars"
    assert body["next_offset"] == 5


def test_long_lines_are_cut(scope: Scope, tmp_path: Path) -> None:
    (tmp_path / "wide.txt").write_text("x" * 5000 + "\nshort\n")
    body = call(scope, str(tmp_path / "wide.txt"))
    first = body["content"].split("\n")[0]
    assert first.startswith("1|xxx") and first.endswith("[line cut]")
    assert len(first) < 2100
    assert baseline(scope, tmp_path / "wide.txt") is None


def test_utf16_is_transcoded_with_a_note(scope: Scope, tmp_path: Path) -> None:
    (tmp_path / "u.txt").write_bytes("héllo\nwörld\n".encode("utf-16"))
    body = call(scope, str(tmp_path / "u.txt"))
    assert body["content"] == "1|héllo\n2|wörld"
    assert body["encoding"].startswith("utf-16")
    (tmp_path / "le.txt").write_bytes("plain ascii here\n".encode("utf-16-le"))
    assert call(scope, str(tmp_path / "le.txt"))["content"] == "1|plain ascii here"


def test_crlf_lines_show_without_carriage_returns(scope: Scope, tmp_path: Path) -> None:
    (tmp_path / "w.txt").write_bytes(b"a\r\nb\r\n")
    assert call(scope, str(tmp_path / "w.txt"))["content"] == "1|a\n2|b"


def test_merge_conflict_blocks_are_counted(scope: Scope, tmp_path: Path) -> None:
    (tmp_path / "c.py").write_text("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> branch\n")
    assert call(scope, str(tmp_path / "c.py"))["conflict_blocks"] == 1


def test_secrets_in_content_are_masked_and_give_no_baseline(scope: Scope, tmp_path: Path) -> None:
    token = "gh" + "p_" + "Z" * 36
    target = tmp_path / "notes.txt"
    target.write_text(f"token here: {token}\n")
    body = call(scope, str(target))
    assert token not in body["content"]
    assert baseline(scope, target) is None


# -- guards ----------------------------------------------------------------------------------------


def test_device_files_and_symlinks_to_them_are_refused(scope: Scope, tmp_path: Path) -> None:
    assert "device" in call(scope, "/dev/zero")["error"]
    (tmp_path / "env").symlink_to("/proc/self/environ")
    assert "device" in call(scope, str(tmp_path / "env"))["error"]


def test_nt_namespace_refused(scope: Scope) -> None:
    assert "namespace" in call(scope, "\\\\?\\GLOBALROOT\\x")["error"]


def test_fifo_is_refused_without_opening(scope: Scope, tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")
    assert "FIFO" in call(scope, str(tmp_path / "pipe"))["error"]


def test_secret_files_are_refused(scope: Scope, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("X=1\n")
    assert "Access refused" in call(scope, str(tmp_path / ".env"))["error"]
    secrets_file = scope.home / "secrets.json"
    secrets_file.write_text("{}")
    assert "secret store" in call(scope, str(secrets_file))["error"]


def test_binary_files_are_refused(scope: Scope, tmp_path: Path) -> None:
    (tmp_path / "pic.png").write_bytes(b"\x89PNG\r\n")
    assert "vision" in call(scope, str(tmp_path / "pic.png"))["error"]
    (tmp_path / "blob.txt").write_bytes(b"abc\x00\x01\x02def")
    assert "binary" in call(scope, str(tmp_path / "blob.txt"))["error"]


def test_files_over_ten_megabytes_are_refused(scope: Scope, tmp_path: Path) -> None:
    big = tmp_path / "big.log"
    with big.open("wb") as handle:
        handle.truncate(11 * 1024 * 1024)
    assert "up to 10 MB" in call(scope, str(big))["error"]


def test_directory_is_refused(scope: Scope, tmp_path: Path) -> None:
    assert "directory" in call(scope, str(tmp_path))["error"]


def test_not_found_suggests_and_is_remembered_until_created(scope: Scope, tmp_path: Path) -> None:
    lines_file(tmp_path, 1, "config_loader.py")
    missing = tmp_path / "config_loder.py"
    first = call(scope, str(missing))
    assert first["error"].startswith("File not found")
    assert str(tmp_path / "config_loader.py") in first["similar_files"]
    assert call(scope, str(missing)) == first
    missing.write_text("now here\n")
    assert call(scope, str(missing))["content"] == "1|now here"


def test_not_found_memory_expires(scope: Scope, tmp_path: Path, fake_clock: Any) -> None:
    missing = str(tmp_path / "gone.txt")
    call(scope, missing)
    tracker = read_tracking.tracker_for(scope, "t1")
    assert tracker.cached_missing(os.path.realpath(missing), scope.clock.monotonic()) is not None
    fake_clock.advance(61)
    assert tracker.cached_missing(os.path.realpath(missing), scope.clock.monotonic()) is None


# -- the loop breaker ------------------------------------------------------------------------------


def test_repeat_read_gets_a_stub_then_a_refusal(scope: Scope, tmp_path: Path) -> None:
    target = lines_file(tmp_path, 3)
    assert "content" in call(scope, str(target))
    stub = call(scope, str(target))
    assert stub["status"] == "unchanged" and "content" not in stub
    text = raw_call(scope, str(target))
    refusal = json.loads(text)
    assert refusal["status"] == "blocked" and refusal["harness_refusal"] is True
    assert ToolResult.ok(text).is_error is False


def test_changed_file_is_served_again(scope: Scope, tmp_path: Path) -> None:
    target = lines_file(tmp_path, 3)
    call(scope, str(target))
    target.write_text("different\n")
    assert call(scope, str(target))["content"] == "1|different"


def test_identical_reads_warn_at_three_and_block_at_four(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "grow.txt"
    bodies = []
    for i in range(4):
        target.write_text(f"version {i}\n")
        bodies.append(call(scope, str(target)))
    assert "warning" not in bodies[1]
    assert "warning" in bodies[2]
    assert bodies[3]["status"] == "blocked" and bodies[3]["harness_refusal"] is True


def test_another_tool_resets_the_consecutive_count(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "grow.txt"
    for i in range(3):
        target.write_text(f"v{i}\n")
        call(scope, str(target))
    read_tracking.note_other_tool(scope, "t1")
    target.write_text("v9\n")
    assert "warning" not in call(scope, str(target))


def test_compaction_boundary_serves_full_content_once(scope: Scope, tmp_path: Path) -> None:
    target = lines_file(tmp_path, 3)
    call(scope, str(target))
    assert call(scope, str(target))["status"] == "unchanged"
    read_tracking.compaction_boundary(scope, "t1")
    assert "content" in call(scope, str(target))
    assert call(scope, str(target))["status"] == "unchanged"


def test_tasks_and_profiles_do_not_share_trackers(scope: Scope, tmp_path: Path) -> None:
    target = lines_file(tmp_path, 3)
    call(scope, str(target), task="a")
    assert "content" in call(scope, str(target), task="b")


# -- baselines -------------------------------------------------------------------------------------


def test_full_read_records_a_baseline_and_partial_does_not(scope: Scope, tmp_path: Path) -> None:
    target = lines_file(tmp_path, 10)
    call(scope, str(target), limit=5)
    assert baseline(scope, target) is None
    call(scope, str(target), task="t1", limit=2000)
    assert baseline(scope, target) == hashlib.sha256(target.read_bytes()).hexdigest()


def test_contiguous_pages_make_a_baseline(scope: Scope, tmp_path: Path) -> None:
    target = lines_file(tmp_path, 10)
    call(scope, str(target), limit=6)
    assert baseline(scope, target) is None
    call(scope, str(target), offset=7, limit=6)
    assert baseline(scope, target) == hashlib.sha256(target.read_bytes()).hexdigest()


def test_pages_of_different_versions_do_not_combine(scope: Scope, tmp_path: Path) -> None:
    target = lines_file(tmp_path, 10)
    call(scope, str(target), limit=6)
    target.write_text(target.read_text() + "extra\n")
    call(scope, str(target), offset=7, limit=6)
    assert baseline(scope, target) is None


# -- other backends and registration ---------------------------------------------------------------


def test_reads_through_a_shell_environment(scope: Scope, tmp_path: Path) -> None:
    lines_file(tmp_path, 3, "remote.txt")
    registry = FakeRegistry(ShellEnv(str(tmp_path), noise="motd\n"))
    text = raw_call(scope, "remote.txt", environments=registry)
    body = json.loads(text)
    assert body["content"] == "1|line 1\n2|line 2\n3|line 3"
    assert "error" in json.loads(raw_call(scope, "nope.txt", environments=registry))


def test_register_tools_marks_read_only_and_unbounded() -> None:
    registry = ToolRegistry()
    register_tools(registry)
    entry = registry.get("read_file")
    assert entry is not None
    assert entry.max_result_chars == float("inf")
    assert entry.traits.read_only and entry.traits.path_arg == "path"
    assert entry.toolset == "file"
    assert READ_FILE_SCHEMA["parameters"]["required"] == ["path"]
