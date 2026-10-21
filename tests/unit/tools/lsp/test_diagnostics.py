"""P24-16: errors an edit introduced, inline or deferred, takeover, caps, the tool-result seam."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from indus.backends.contract import BackendFlags
from indus.tools.builtin import lint
from indus.tools.calls import ToolCall, ToolContext, ToolResult
from indus.tools.lsp import diagnostics
from indus.tools.lsp.diagnostics import (
    BASELINE_CAP,
    CHECK_CEILING_S,
    MAX_OWNERS,
    MAX_PER_OWNER,
    TURN_END_WAIT_S,
    WRITE_BUDGET_S,
    LspToolHooks,
    bind_owner,
    format_errors,
    line_map,
    shift_all,
    turn_edits,
)
from tests.conftest import FAKE_CLOCK_START
from tests.fakes.clock import FakeClock, checkpoint
from tests.unit.tools.lsp._helpers import REAL, wait_until
from tests.unit.tools.lsp._scripted import checker, verdict_for


@pytest.fixture(autouse=True)
def no_active_checker() -> Iterator[None]:
    diagnostics.install_active(None)
    yield
    diagnostics.install_active(None)


def test_numbers_match_the_row() -> None:
    assert WRITE_BUDGET_S == 0.08
    assert BASELINE_CAP == 256
    assert (MAX_OWNERS, MAX_PER_OWNER) == (64, 32)
    assert TURN_END_WAIT_S == 5.0
    assert CHECK_CEILING_S == 60.0


# -- the line map ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ("a\nb\nc\n", "a\nb\nc\n", [0, 1, 2]),
        ("a\nb\nc\n", "new\na\nb\nc\n", [1, 2, 3]),
        ("a\nb\nc\n", "a\nc\n", [0, None, 1]),
        ("a\nb\nc\n", "a\nB\nc\n", [0, None, 2]),
        ("a\nb\nc\n", "a\nb\nc\nd\ne\n", [0, 1, 2]),
    ],
)
def test_line_map_table(before: str, after: str, expected: list[int | None]) -> None:
    mapping = line_map(before, after)
    assert [mapping(n) for n in range(3)] == expected


def test_shift_drops_deleted_and_collapses_a_cut_end() -> None:
    before = "a\nb\nc\nd\n"
    after = "a\nd\n"
    items = [
        {"range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 1}}},
        {"range": {"start": {"line": 0, "character": 2}, "end": {"line": 2, "character": 0}}},
        {"range": {"start": {"line": 3, "character": 0}, "end": {"line": 3, "character": 1}}},
    ]
    moved = shift_all(items, line_map(before, after))
    assert [(m["range"]["start"]["line"], m["range"]["end"]["line"]) for m in moved] == [
        (0, 0),
        (1, 1),
    ]


def test_format_lists_one_based_positions() -> None:
    text = format_errors("fake-ls", "/w/a.fk", verdict_for("x\n  BAD(q)\n"))
    assert text.splitlines() == ["fake-ls reports 1 new error in /w/a.fk:", "  2:3 bad q"]


# -- inline verdicts ------------------------------------------------------------------------------


def test_new_error_reported_and_preexisting_one_not(tmp_path: Path) -> None:
    path = str(tmp_path / "a.fk")

    async def scenario() -> None:
        check, _ = checker()
        moved = await check.check_edit(path, "a\nBAD(old)\n", "new line\na\nBAD(old)\n", owner="s")
        assert moved.status == "inline" and moved.errors == ()
        added = await check.check_edit(
            path, "new line\na\nBAD(old)\n", "new line\na\nBAD(old)\nBAD(new)\n", owner="s"
        )
        assert added.status == "inline"
        assert [e["message"] for e in added.errors] == ["bad new"]
        assert check.take_notes("s") == []  # given inline: never again as a note

    asyncio.run(scenario())


def test_same_message_on_a_second_line_is_new_and_warnings_are_quiet(tmp_path: Path) -> None:
    path = str(tmp_path / "a.fk")

    async def scenario() -> None:
        check, _ = checker()
        again = await check.check_edit(path, "BAD(x)\n", "BAD(x)\nBAD(x)\n", owner="s")
        assert [e["range"]["start"]["line"] for e in again.errors] == [1]
        quiet = await check.check_edit(path, "a\n", "a\nWARN(w)\n", owner="s")
        assert quiet.status == "inline" and quiet.errors == ()
        new_file = await check.check_edit(
            str(tmp_path / "n.fk"), None, "BAD(a)\nBAD(b)\n", owner="s"
        )
        assert len(new_file.errors) == 2

    asyncio.run(scenario())


def test_cached_verdict_is_the_next_baseline(tmp_path: Path) -> None:
    path = str(tmp_path / "a.fk")

    async def scenario() -> None:
        check, manager = checker()
        await check.check_edit(path, None, "BAD(one)\n", owner="s")
        manager.client.asked.clear()
        found = await check.check_edit(path, "BAD(one)\n", "BAD(one)\nBAD(two)\n", owner="s")
        assert [e["message"] for e in found.errors] == ["bad two"]
        assert manager.client.asked == ["BAD(one)\nBAD(two)\n"]  # no second round trip
        assert check.cached_paths() == [path]

    asyncio.run(scenario())


def test_no_verdict_is_not_clean(tmp_path: Path) -> None:
    path = str(tmp_path / "a.fk")

    async def scenario() -> None:
        check, manager = checker()
        manager.client.silent = True
        outcome = await check.check_edit(path, "a\n", "BAD(z)\n", owner="s")
        assert outcome.status == "none" and outcome.text(path) is None
        assert await check.settle("s") == []

    asyncio.run(scenario())


def test_server_left_holding_the_newest_text(tmp_path: Path) -> None:
    target = tmp_path / "a.fk"

    async def scenario() -> None:
        check, manager = checker()
        await check.check_edit(str(target), "BAD(a)\n", "BAD(a)\nBAD(b)\n", owner="s")
        assert manager.client.synced_text(str(target)) == "BAD(a)\nBAD(b)\n"

    asyncio.run(scenario())


# -- deferred verdicts ----------------------------------------------------------------------------


def test_late_verdict_rides_the_next_tool_result_once(tmp_path: Path) -> None:
    path = str(tmp_path / "a.fk")

    async def scenario() -> None:
        check, manager = checker()
        diagnostics.install_active(check)
        manager.client.gate = asyncio.Event()
        started = REAL.monotonic()
        outcome = await check.check_edit(path, "a\n", "a\nBAD(late)\n", owner="sess")
        assert outcome.status == "deferred"
        assert REAL.monotonic() - started < 1.0
        manager.client.gate.set()
        await wait_until(lambda: check.running("sess") == 0)
        hooks = LspToolHooks()
        ctx = ToolContext(session_id="sess", turn_id="t1")
        call = ToolCall("terminal", {}, "c1")
        text_result = await hooks.transform(call, ctx, ToolResult.ok("done"))
        assert text_result is not None and "bad late" in text_result.text
        assert "[Language server, about earlier edits]" in text_result.text
        assert await hooks.transform(call, ctx, ToolResult.ok("done")) is None  # delivered once
        other = await hooks.transform(call, ToolContext(session_id="other"), ToolResult.ok("x"))
        assert other is None

    asyncio.run(scenario())


def test_json_results_get_a_key(tmp_path: Path) -> None:
    async def scenario() -> None:
        check, _ = checker()
        diagnostics.install_active(check)
        check.post("s", str(tmp_path / "a.fk"), "fake-ls reports 1 new error", None)
        result = await LspToolHooks().transform(
            ToolCall("read_file", {}, "c"),
            ToolContext(session_id="s"),
            ToolResult.ok(json.dumps({"content": "x"})),
        )
        assert result is not None
        assert json.loads(result.text)["lsp_diagnostics"] == ["fake-ls reports 1 new error"]

    asyncio.run(scenario())


def test_notes_are_capped_per_owner_and_owners(tmp_path: Path) -> None:
    check, _ = checker()
    for n in range(MAX_PER_OWNER + 1):
        check.post("one", f"/w/f{n}.fk", f"note {n}", None)
    notes = check.take_notes("one")
    assert len(notes) == MAX_PER_OWNER and notes[0] == "note 1"
    for n in range(MAX_OWNERS + 1):
        check.post(f"owner{n}", "/w/a.fk", "x", None)
    assert len(check.owners()) == MAX_OWNERS and "owner0" not in check.owners()


def test_second_edit_by_same_session_keeps_first_baseline(tmp_path: Path) -> None:
    path = str(tmp_path / "a.fk")

    async def scenario() -> None:
        check, manager = checker()
        gate = manager.client.gate = asyncio.Event()
        first = await check.check_edit(path, "a\n", "a\nBAD(first)\n", owner="s")
        second = await check.check_edit(path, "a\nBAD(first)\n", "a\nBAD(first)\nb\n", owner="s")
        assert (first.status, second.status) == ("deferred", "deferred")
        gate.set()
        notes = await check.settle("s")
        assert len(notes) == 1 and "bad first" in notes[0]
        fixed_gate = manager.client.gate = asyncio.Event()
        await check.check_edit(path, "a\n", "a\nBAD(again)\n", owner="s")
        await check.check_edit(path, "a\nBAD(again)\n", "a\n", owner="s")  # fixed by the second
        fixed_gate.set()
        assert await check.settle("s") == []

    asyncio.run(scenario())


def test_no_verdict_restores_the_older_note(tmp_path: Path) -> None:
    path = str(tmp_path / "a.fk")

    async def scenario() -> None:
        check, manager = checker()
        gate = manager.client.gate = asyncio.Event()
        await check.check_edit(path, "a\n", "a\nBAD(kept)\n", owner="s")
        gate.set()
        await wait_until(lambda: check.running("s") == 0)
        manager.client.gate = None
        manager.client.silent = True
        outcome = await check.check_edit(path, "a\nBAD(kept)\n", "a\nBAD(kept)\nc\n", owner="s")
        assert outcome.status == "none"
        notes = await check.settle("s")
        assert len(notes) == 1 and "bad kept" in notes[0]

    asyncio.run(scenario())


def test_checks_of_one_file_run_one_at_a_time(tmp_path: Path) -> None:
    path = str(tmp_path / "a.fk")

    async def scenario() -> None:
        check, manager = checker()
        gate = manager.client.gate = asyncio.Event()
        await check.check_edit(path, "a\n", "a\nBAD(x)\n", owner="one")
        await check.check_edit(path, "a\nBAD(x)\n", "a\nBAD(x)\nBAD(y)\n", owner="two")
        for _ in range(20):
            await checkpoint()
        assert manager.client.active == 1
        gate.set()
        one = await check.settle("one")
        two = await check.settle("two")
        assert manager.client.most_active == 1
        assert "bad x" in one[0] and "bad y" in two[0] and "bad x" not in two[0]

    asyncio.run(scenario())


def test_check_ceiling_marks_the_pair_broken(tmp_path: Path) -> None:
    path = str(tmp_path / "a.fk")
    clock = FakeClock(FAKE_CLOCK_START)

    async def scenario() -> None:
        check, manager = checker(clock)
        manager.client.hang = True
        task = asyncio.ensure_future(check.check_edit(path, "a\n", "BAD(h)\n", owner="s"))
        await clock.run_until_idle()
        clock.advance(WRITE_BUDGET_S)
        assert (await task).status == "deferred"
        await clock.advance_and_settle(CHECK_CEILING_S - 1)
        assert manager.dropped == []
        await clock.advance_and_settle(1)
        assert [p for p, _ in manager.dropped] == [path]
        assert check.running("s") == 0 and check.take_notes("s") == []

    asyncio.run(scenario())


def test_settle_waits_at_most_five_seconds(tmp_path: Path) -> None:
    path = str(tmp_path / "a.fk")
    clock = FakeClock(FAKE_CLOCK_START)

    async def scenario() -> None:
        check, manager = checker(clock)
        manager.client.gate = asyncio.Event()
        task = asyncio.ensure_future(check.check_edit(path, "a\n", "BAD(s)\n", owner="s"))
        await clock.run_until_idle()
        clock.advance(WRITE_BUDGET_S)
        await task
        settle = asyncio.ensure_future(check.settle("s"))
        await clock.run_until_idle()
        await clock.advance_and_settle(TURN_END_WAIT_S - 0.1)
        assert not settle.done()
        await clock.advance_and_settle(0.2)
        assert settle.done() and settle.result() == []
        manager.client.gate.set()
        await clock.run_until_idle()
        assert len(check.take_notes("s")) == 1  # it lands later, for the next turn

    asyncio.run(scenario())


# -- the lint report ------------------------------------------------------------------------------


class LocalHost:
    flags = BackendFlags(host_local=True)

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd

    async def execute(self, *a: object, **k: object) -> object:  # never asked for .fk files
        raise AssertionError("no shell checker for this suffix")


class ContainerHost(LocalHost):
    flags = BackendFlags(isolated=True, container_paths=True)


def test_lint_report_gains_the_inline_lsp_errors(tmp_path: Path) -> None:
    async def scenario() -> None:
        check, _ = checker()
        diagnostics.install_active(check)
        with bind_owner("s", "t1"):
            report = await lint.lint_edit(
                "a.fk", "a\n", "a\nBAD(new)\n", host=cast(Any, LocalHost(str(tmp_path)))
            )
            clean = await lint.lint_edit(str(tmp_path / "b.fk"), "a\n", "b\n")
        assert report.status == "introduced" and report.linter == "lsp:fake-ls"
        assert report.errors == ("fake-ls reports 1 new error in a.fk:", "  2:1 bad new")
        assert clean.status == "skipped"
        assert turn_edits("s", "t1") == [str(tmp_path / "a.fk"), str(tmp_path / "b.fk")]

    asyncio.run(scenario())


def test_python_errors_and_lsp_errors_are_merged(tmp_path: Path) -> None:
    async def scenario() -> None:
        check, manager = checker()
        diagnostics.install_active(check)
        manager.handles = lambda path: True  # type: ignore[method-assign]
        manager.client_for = _always(manager.client)  # type: ignore[method-assign]
        report = await lint.lint_edit(str(tmp_path / "m.py"), "x = 1\n", "x = (\nBAD(t)\n")
        assert report.status == "introduced" and report.linter == "python+lsp:fake-ls"
        assert any("SyntaxError" in e for e in report.errors)
        assert any("bad t" in e for e in report.errors)

    asyncio.run(scenario())


def _always(client: object):  # noqa: ANN202
    async def found(path: str) -> object:
        return client

    return found


def test_edits_in_a_container_are_not_checked(tmp_path: Path) -> None:
    async def scenario() -> None:
        check, manager = checker()
        diagnostics.install_active(check)
        with bind_owner("s", "t2"):
            report = await lint.lint_edit(
                "/work/a.fk", "a\n", "BAD(x)\n", host=cast(Any, ContainerHost("/work"))
            )
        assert report.status == "skipped" and manager.client.asked == []
        assert turn_edits("s", "t2") == []

    asyncio.run(scenario())


def test_lint_without_a_checker_is_unchanged(tmp_path: Path) -> None:
    async def scenario() -> None:
        report = await lint.lint_edit(str(tmp_path / "a.json"), None, "{}")
        assert report.status == "clean" and report.linter == "json"

    asyncio.run(scenario())


def test_real_server_end_to_end(tmp_path: Path) -> None:
    """Fake server over stdio, real manager: a cold start defers, a warm one answers inline."""
    import os

    from indus.foundation.clock import SystemClock
    from indus.tools.lsp.diagnostics import EditDiagnostics
    from indus.tools.lsp.manager import LspManager, LspSettings
    from indus.tools.lsp.servers import ServerSpec
    from tests.unit.tools.lsp._helpers import git_tree, server_argv

    repo = git_tree(tmp_path / "repo")
    target = repo / "m.fk"
    spec = ServerSpec(
        "fake", (".fk",), (), command=tuple(server_argv(tmp_path)), project_code=False
    )

    async def scenario() -> None:
        manager = LspManager(
            LspSettings(servers=(spec,)),
            clock=SystemClock(),
            env_factory=lambda extra: {**os.environ, **extra},
        )
        check = EditDiagnostics(manager)
        cold = await check.check_edit(str(target), "a\n", "a\nBAD(cold)\n", owner="e2e")
        notes = await check.settle("e2e") if cold.status == "deferred" else []
        found = notes[0] if notes else (cold.text(str(target)) or "")
        assert "bad cold" in found
        warm = await check.check_edit(
            str(target), "a\nBAD(cold)\n", "a\nBAD(cold)\nBAD(warm)\n", owner="e2e"
        )
        later = await check.settle("e2e")
        text = warm.text(str(target)) or "\n".join(later)
        assert "bad warm" in text and "bad cold" not in text
        await manager.close()

    asyncio.run(scenario())
