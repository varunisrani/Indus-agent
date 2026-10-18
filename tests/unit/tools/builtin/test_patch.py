"""The ``patch`` tool (P05-19): diff and strategy, guards, line endings, misses."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.tools.builtin import read_tracking
from indus.tools.builtin.patch import PATCH_SCHEMA, PatchFiles, patch, register_tools
from indus.tools.registry import ToolRegistry
from tests.unit.tools.builtin._file_env import FakeRegistry, ShellEnv


@pytest.fixture(autouse=True)
def fresh_trackers() -> Iterator[None]:
    read_tracking.reset_all()
    yield
    read_tracking.reset_all()


def edit(scope: Scope, path: object, old: object, new: object, **extra: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"path": path, "old_string": old, "new_string": new}
    kwargs = {k: extra.pop(k) for k in ("environments", "broker") if k in extra}
    args.update(extra)
    return json.loads(asyncio.run(patch(args, scope=scope, task_id="t1", **kwargs)))


def test_exact_edit_returns_diff_and_strategy(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("def main():\n    print('hi')\n")
    body = edit(scope, str(target), "print('hi')", "print('hello')")
    assert body["success"] is True and body["strategy"] == "exact"
    assert "-    print('hi')" in body["diff"] and "+    print('hello')" in body["diff"]
    assert body["verified"] is True and body["files_modified"] == [str(target)]
    assert target.read_text() == "def main():\n    print('hello')\n"


def test_fuzzy_edit_names_its_strategy(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("if x:\n        run(  a,  b )\n")
    body = edit(scope, str(target), "run( a, b )", "run(a)")
    assert body["strategy"] == "whitespace_normalized"
    assert target.read_text() == "if x:\n        run(a)\n"


def test_crlf_and_bom_survive_a_patch(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "win.txt"
    target.write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\nthree\r\n")
    edit(scope, str(target), "one\ntwo", "uno\ndos")
    assert target.read_bytes() == b"\xef\xbb\xbfuno\r\ndos\r\nthree\r\n"


def test_replace_all_flag_as_text(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a a a\n")
    body = edit(scope, str(target), "a", "b", replace_all="true")
    assert body["replacements"] == 3 and target.read_text() == "b b b\n"


def test_ambiguous_edit_is_an_error_and_nothing_changes(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("x = 1\nx = 1\n")
    body = edit(scope, str(target), "x = 1", "x = 2")
    assert "matches 2 places" in body["error"]
    assert target.read_text() == "x = 1\nx = 1\n"


def test_already_applied_is_a_no_change_success(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "f.py"
    target.write_text("timeout = 30\n")
    before = target.stat().st_mtime_ns
    body = edit(scope, str(target), "timeout = 10", "timeout = 30")
    assert body["success"] is True and body["no_change"] is True
    assert target.stat().st_mtime_ns == before


def test_repeated_misses_get_stronger_advice(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "f.py"
    target.write_text("nothing similar in here\n")
    bodies = [edit(scope, str(target), f"zzz qqq {i} vvv www", "x") for i in range(3)]
    assert "hint" not in bodies[0]
    assert "miss number 3" in bodies[2]["hint"]


def test_missing_file_points_to_write_file(scope: Scope, tmp_path: Path) -> None:
    assert "use write_file" in edit(scope, str(tmp_path / "nope.py"), "a", "b")["error"]


def test_bad_arguments(scope: Scope, tmp_path: Path) -> None:
    assert "path is required" in edit(scope, "", "a", "b")["error"]
    assert "both required" in edit(scope, str(tmp_path / "f"), None, "b")["error"]
    assert "not available" in edit(scope, str(tmp_path / "f"), "a", "b", mode="weird")["error"]
    no_text = edit(scope, str(tmp_path / "f"), "a", "b", mode="patch")
    assert no_text["success"] is False and "needs the patch text" in no_text["error"]


def test_patch_goes_through_the_write_guards(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_text("rules\n")
    body = edit(scope, str(target), "rules", "new rules")
    assert "approval" in body["error"]
    assert target.read_text() == "rules\n"
    assert "system path" in edit(scope, "/etc/hosts", "a", "b")["error"]
    (tmp_path / "r.docx").write_bytes(b"PK")
    assert "document" in edit(scope, str(tmp_path / "r.docx"), "a", "b")["error"]


def test_patch_through_a_shell_environment(scope: Scope, tmp_path: Path) -> None:
    (tmp_path / "remote.py").write_text("value = 1\n")
    registry = FakeRegistry(ShellEnv(str(tmp_path), noise="hello from the box\n"))
    body = edit(scope, "remote.py", "value = 1", "value = 2", environments=registry)
    assert body["success"] is True, body
    assert (tmp_path / "remote.py").read_text() == "value = 2\n"


def test_register_tools_and_schema() -> None:
    registry = ToolRegistry()
    register_tools(registry)
    entry = registry.get("patch")
    assert entry is not None and entry.traits.writer and entry.toolset == "file"
    assert PATCH_SCHEMA["parameters"]["required"] == ["path", "old_string", "new_string"]


# -- the V4A adapter (P05-20) and the lint hook --------------------------------------------------


def adapter(scope: Scope, tmp_path: Path, **kwargs: Any) -> PatchFiles:
    from indus.tools.builtin.file_ops import HostFileOps

    ops = HostFileOps(str(tmp_path))
    return PatchFiles(ops, scope=scope, tracker=read_tracking.tracker_for(scope, "t1"), **kwargs)


def test_patch_files_adapter_round_trip(scope: Scope, tmp_path: Path) -> None:
    files = adapter(scope, tmp_path)
    (tmp_path / "a.txt").write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\n")
    assert files.resolve("a.txt") == str(tmp_path / "a.txt")
    assert asyncio.run(files.read_text("a.txt")) == "one\r\ntwo\r\n"
    asyncio.run(files.write_text("a.txt", "uno\ndos\n"))
    assert (tmp_path / "a.txt").read_bytes() == b"\xef\xbb\xbfuno\r\ndos\r\n"
    asyncio.run(files.write_text("new/b.txt", "fresh\n"))
    tracker = read_tracking.tracker_for(scope, "t1")
    assert tracker.baseline(str(tmp_path / "new" / "b.txt")) is not None
    assert tracker.baseline(str(tmp_path / "a.txt")) is None  # written without a full read
    asyncio.run(files.delete("new/b.txt"))
    assert not (tmp_path / "new" / "b.txt").exists()
    assert asyncio.run(files.read_text("new/b.txt")) is None


def test_patch_files_refusal_runs_the_write_guards(scope: Scope, tmp_path: Path) -> None:
    files = adapter(scope, tmp_path)
    assert asyncio.run(files.refusal("/etc/hosts", "write", "x"))
    assert asyncio.run(files.refusal("/etc/hosts", "delete", None))
    assert asyncio.run(files.refusal("doc.docx", "write", "text"))
    assert asyncio.run(files.refusal("doc.docx", "delete", None)) is None
    assert asyncio.run(files.refusal("AGENTS.md", "write", "rules"))
    assert asyncio.run(files.refusal("src/main.py", "write", "x = 1\n")) is None


def test_lint_gate_refuses_breaking_a_data_file(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "conf.json"
    target.write_text('{"a": 1}\n')
    body = edit(scope, str(target), '"a": 1', '"a": ')
    assert "not valid JSON" in body["error"]
    assert target.read_text() == '{"a": 1}\n'


def test_lint_after_reports_only_errors_the_edit_introduced(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("x = 1\n")
    clean = edit(scope, str(target), "x = 1", "x = 2")
    assert clean["success"] is True and "lint" not in clean
    broken = edit(scope, str(target), "x = 2", "x = (")
    assert broken["success"] is True and broken["lint"]["status"] == "introduced"
    assert broken["lint"]["linter"] == "python" and broken["lint"]["errors"]
    assert target.read_text() == "x = (\n"


# -- mode patch: V4A through the tool (P05-20 integration) -------------------------------------

V4A_TWO_FILES = """*** Begin Patch
*** Update File: a.py
@@
-value = 1
+value = 2
*** Add File: pkg/b.py
+print("new")
*** End Patch
"""


def v4a_call(scope: Scope, text: str, **kwargs: Any) -> dict[str, Any]:
    args = {"mode": "patch", "patch": text}
    return json.loads(asyncio.run(patch(args, scope=scope, task_id="t1", **kwargs)))


def test_patch_mode_applies_a_multi_file_patch(scope: Scope, tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("value = 1\n")
    body = v4a_call(
        scope,
        V4A_TWO_FILES.replace("a.py", str(tmp_path / "a.py")).replace(
            "pkg/b.py", str(tmp_path / "pkg" / "b.py")
        ),
    )
    assert body["success"] is True, body
    assert (tmp_path / "a.py").read_text() == "value = 2\n"
    assert (tmp_path / "pkg" / "b.py").read_text() == 'print("new")\n'
    assert "+value = 2" in body["diff"]
    tracker = read_tracking.tracker_for(scope, "t1")
    assert tracker.baseline(str(tmp_path / "pkg" / "b.py")) is not None  # own new file


def test_patch_mode_is_taken_without_a_mode_when_only_patch_text_is_sent(
    scope: Scope, tmp_path: Path
) -> None:
    (tmp_path / "a.py").write_text("value = 1\n")
    text = V4A_TWO_FILES.replace("a.py", str(tmp_path / "a.py")).replace(
        "pkg/b.py", str(tmp_path / "b.py")
    )
    body = json.loads(asyncio.run(patch({"patch": text}, scope=scope, task_id="t1")))
    assert body["success"] is True and (tmp_path / "b.py").exists()


def test_patch_mode_write_guards_stop_every_file(scope: Scope, tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("value = 1\n")
    (tmp_path / "AGENTS.md").write_text("rules\n")
    text = (
        "*** Begin Patch\n"
        f"*** Update File: {tmp_path / 'a.py'}\n@@\n-value = 1\n+value = 2\n"
        f"*** Update File: {tmp_path / 'AGENTS.md'}\n@@\n-rules\n+new rules\n"
        "*** End Patch\n"
    )
    body = v4a_call(scope, text)
    assert body["success"] is False and "approval" in body["error"]
    assert (tmp_path / "a.py").read_text() == "value = 1\n"
    assert (tmp_path / "AGENTS.md").read_text() == "rules\n"


def test_patch_mode_fail_closed_parse_rule(scope: Scope, tmp_path: Path) -> None:
    (tmp_path / "c.json").write_text('{"a": 1}\n')
    text = (
        "*** Begin Patch\n"
        f'*** Update File: {tmp_path / "c.json"}\n@@\n-{{"a": 1}}\n+{{"a": }}\n'
        "*** End Patch\n"
    )
    body = v4a_call(scope, text)
    assert body["success"] is False and "not valid JSON" in body["error"]
    assert (tmp_path / "c.json").read_text() == '{"a": 1}\n'


def test_patch_mode_through_a_shell_environment(scope: Scope, tmp_path: Path) -> None:
    (tmp_path / "remote.py").write_text("value = 1\n")
    registry = FakeRegistry(ShellEnv(str(tmp_path), noise="hello from the box\n"))
    text = (
        "*** Begin Patch\n*** Update File: remote.py\n@@\n-value = 1\n+value = (\n*** End Patch\n"
    )
    body = v4a_call(scope, text, environments=registry)
    assert body["success"] is True, body
    assert (tmp_path / "remote.py").read_text() == "value = (\n"
    assert body["lint"]["remote.py"]["status"] == "introduced"
