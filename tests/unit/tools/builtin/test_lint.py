"""Lint after edit: in-process checkers, the fail-closed gate, and shell checkers (P05-20)."""

from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from indus.backends.local import LocalEnvironment
from indus.foundation.scope import Scope
from indus.tools.builtin.lint import check_text, gate, lint_edit


@dataclass(frozen=True)
class Out:
    output: str
    returncode: int = 0
    timed_out: bool = False
    interrupted: bool = False


class FakeHost:
    """Answers each command with a function of (command, stdin)."""

    def __init__(self, answer: Callable[[str, str | None], Out]) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str | None, bool]] = []

    @property
    def cwd(self) -> str:
        return "/work"

    async def execute(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        stdin: str | None = None,
        bounded: bool = True,
        cancel: Any = None,
    ) -> Out:
        self.calls.append((command, stdin, bounded))
        return self.answer(command, stdin)


def _lint(path: str, before: str | None, after: str, host: Any = None) -> dict[str, Any]:
    return asyncio.run(lint_edit(path, before, after, host=host)).as_dict()


# -- in-process -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "good", "bad", "word"),
    [
        ("m.py", "x = 1\n", "def f(:\n", "SyntaxError"),
        ("d.json", '{"a": 1}', '{"a": }', "invalid JSON"),
        ("c.yaml", "a: 1\n---\nb: !Ref x\n", "a: [1,\nb: 2\n", "invalid YAML"),
        ("c.yml", "k: v\n", "k: 'open\n", "invalid YAML"),
        ("p.toml", "[a]\nb = 1\n", "[a\nb = 1\n", "invalid TOML"),
    ],
)
def test_in_process_checkers(path: str, good: str, bad: str, word: str) -> None:
    assert check_text(path, good) == []
    problems = check_text(path, bad)
    assert problems and word in problems[0]


def test_python_compile_never_runs_code(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    source = f"open({str(marker)!r}, 'w').write('x')\n"
    assert check_text("x.py", source) == []
    assert not marker.exists()


def test_unknown_suffix_has_no_checker() -> None:
    assert check_text("notes.txt", "anything") is None
    assert _lint("notes.txt", None, "x")["status"] == "skipped"


def test_new_python_error_reported_and_old_one_not() -> None:
    assert _lint("m.py", "x = 1\n", "x = 2\n") == {"status": "clean", "linter": "python"}
    new = _lint("m.py", "x = 1\n", "x = (\n")
    assert new["status"] == "introduced" and "m.py:1" in new["errors"][0]
    # the same problem moved two lines down by the edit is still the old one
    old = _lint("m.py", "def f(:\n    pass\n", "# a\n# b\ndef f(:\n    pass\n")
    assert old["status"] == "preexisting" and old["existing"] == 1
    assert "errors" not in old


def test_new_file_with_a_problem_is_introduced() -> None:
    report = _lint("n.json", None, "{")
    assert report["status"] == "introduced" and report["linter"] == "json"


# -- gate -------------------------------------------------------------------------------------


def test_gate_refuses_breaking_a_valid_data_file() -> None:
    reason = gate("cfg.json", '{"a": 1}', '{"a": ')
    assert reason is not None and "not valid JSON" in reason and "nothing was written" in reason
    assert gate("new.toml", None, "[x") is not None


def test_gate_allows_valid_content_already_broken_files_and_other_suffixes() -> None:
    assert gate("cfg.json", '{"a": 1}', '{"a": 2}') is None
    assert gate("cfg.yaml", "a: [\n", "a: [\nb: 1\n") is None
    assert gate("m.py", "x = 1\n", "x = (\n") is None  # python is reported, never refused


# -- shell checkers ---------------------------------------------------------------------------


def test_shell_linter_missing_tool_is_skipped() -> None:
    host = FakeHost(lambda cmd, stdin: Out("__INDUS_LINT_TOOL_MISSING__\n", 127))
    report = _lint("app.js", "a", "b", host)
    assert report == {
        "status": "skipped",
        "linter": "node",
        "note": "node is not installed in this environment",
    }
    assert host.calls[0][2] is False  # helper commands are never bounded (R02.2)


def test_shell_linter_without_a_host_is_skipped() -> None:
    assert _lint("app.ts", None, "let x = 1")["status"] == "skipped"


def test_shell_linter_clean_runs_once() -> None:
    host = FakeHost(lambda cmd, stdin: Out(""))
    assert _lint("/work/app.js", "a", "b", host)["status"] == "clean"
    assert len(host.calls) == 1 and "node --check /work/app.js" in host.calls[0][0]


def test_shell_linter_compares_with_old_content_in_a_temp_sibling() -> None:
    def answer(cmd: str, stdin: str | None) -> Out:
        if stdin is None:  # the edited file
            return Out("/work/app.js:9\nOld\n/work/app.js:3\nSyntaxError: Unexpected token\n", 1)
        temp = re.search(r"(\.indus-lint-[0-9a-f]+-app\.js)", cmd)
        assert temp is not None and "rm -f" in cmd
        return Out(f"/work/{temp.group(1)}:7\nOld\n", 1)

    host = FakeHost(answer)
    report = _lint("/work/app.js", "old text", "new text", host)
    assert report["status"] == "introduced"
    assert report["errors"] == ["/work/app.js:3", "SyntaxError: Unexpected token"]
    assert report["existing"] == 2
    assert host.calls[1][1] == "old text"


def test_shell_linter_all_old_problems_is_preexisting() -> None:
    def answer(cmd: str, stdin: str | None) -> Out:
        return Out("error TS1005: ';' expected.\n", 2)

    report = _lint("/work/x.ts", "a", "b", FakeHost(answer))
    assert report["status"] == "preexisting" and report["linter"] == "tsc"


def test_shell_linter_timeout_is_skipped_and_old_content_failure_lists_all() -> None:
    timed = FakeHost(lambda cmd, stdin: Out("", 124, timed_out=True))
    assert _lint("/work/a.js", "a", "b", timed)["status"] == "skipped"

    def answer(cmd: str, stdin: str | None) -> Out:
        if stdin is None:
            return Out("boom\n", 1)
        return Out("__INDUS_LINT_NO_TEMP__\n", 126)

    report = _lint("/work/a.js", "a", "b", FakeHost(answer))
    assert report["status"] == "introduced" and report["errors"] == ["boom"]
    assert "Could not lint the old content" in report["note"]


def test_host_that_raises_never_breaks_the_edit() -> None:
    def answer(cmd: str, stdin: str | None) -> Out:
        raise RuntimeError("link lost")

    assert _lint("/work/a.js", "a", "b", FakeHost(answer))["status"] == "skipped"


@pytest.mark.skipif(
    shutil.which("node") is None or shutil.which("bash") is None, reason="needs node and bash"
)
def test_real_node_check_reports_only_new_errors(scope: Scope, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    temp = tmp_path / "t"
    temp.mkdir()
    target = work / "app.js"

    async def go(before: str, after: str) -> dict[str, Any]:
        target.write_text(after)
        env = LocalEnvironment(scope, cwd=str(work), timeout=20, temp_dir=str(temp))
        await env.start()
        try:
            report = await lint_edit(str(target), before, after, host=env)
        finally:
            await env.cleanup()
        return report.as_dict()

    clean = asyncio.run(go("const a = ;\n", "const a = 1;\n"))
    assert clean == {"status": "clean", "linter": "node"}
    new = asyncio.run(go("const a = 1;\n", "const a = ;\n"))
    assert new["status"] == "introduced", new
    assert any("SyntaxError" in line for line in new["errors"])
    # node stops at the first error; moved two lines down it is still the old one
    old = asyncio.run(go("const a = ;\n", "// one\n// two\nconst a = ;\n"))
    assert old["status"] == "preexisting", old
    assert sorted(p.name for p in work.iterdir()) == ["app.js"]  # temp sibling removed


def test_data_checkers_accept_a_byte_order_mark() -> None:
    assert check_text("d.json", '﻿{"a": 1}') == []
    assert check_text("p.toml", "﻿a = 1\n") == []


@pytest.mark.skipif(
    shutil.which("tsc") is None or shutil.which("bash") is None, reason="needs tsc and bash"
)
def test_real_tsc_is_skipped_inside_a_tsconfig_project(scope: Scope, tmp_path: Path) -> None:
    work = tmp_path / "proj"
    (work / "src").mkdir(parents=True)
    temp = tmp_path / "t"
    temp.mkdir()
    (work / "tsconfig.json").write_text("{}\n")
    target = work / "src" / "a.ts"
    target.write_text("import x from '@/nowhere';\n")

    async def go() -> dict[str, Any]:
        env = LocalEnvironment(scope, cwd=str(work), timeout=20, temp_dir=str(temp))
        await env.start()
        try:
            report = await lint_edit(str(target), None, target.read_text(), host=env)
        finally:
            await env.cleanup()
        return report.as_dict()

    report = asyncio.run(go())
    assert report["status"] == "skipped" and "tsconfig.json" in report["note"]
