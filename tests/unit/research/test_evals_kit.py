"""P22-15: the eval kit (resume-safe cells, one-variable arms, oracles, trace metrics, copies
only, observed vs modelled). ACC-O8-1, ACC-O8-2."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from indus.research.evals.kit import (
    Arm,
    ArmError,
    Cell,
    Figure,
    LiveStoreError,
    MixedLabels,
    Report,
    ResultsStore,
    RunOutcome,
    cell_id,
    cells,
    check_arms,
    file_check,
    marker,
    matches,
    open_store_copy,
    read_session_rows,
    total,
    trace_metrics,
)


def test_cell_ids_are_stable_and_distinct() -> None:
    assert cell_id("base", "t1", 0) == cell_id("base", "t1", 0)
    ids = {Cell(a, t, r).id for a in ("base", "fix") for t in ("t1", "t2") for r in range(3)}
    assert len(ids) == 12 and all(len(i) == 16 for i in ids)


def test_rerun_skips_done_cells_and_retries_a_crashed_one(tmp_path: Path) -> None:
    store = ResultsStore(tmp_path / "exp")
    plan = cells(["base", "fix"], ["t1", "t2"], 2)
    ran: list[str] = []
    crash_once = {Cell("fix", "t2", 1).id}

    def run_cell(cell: Cell) -> RunOutcome:
        ran.append(cell.id)
        if cell.id in crash_once:
            crash_once.clear()
            return RunOutcome(exit_code=1, output="  \n")  # a startup crash: not recorded
        return RunOutcome(exit_code=0, output=f"answer {cell.task}", data={"wall_s": 1.5})

    recorded, crashed = store.run(plan, run_cell)
    assert len(recorded) == 7 and crashed == [Cell("fix", "t2", 1)]
    assert len(store.rows()) == 7
    ran.clear()
    recorded, crashed = store.run(plan, run_cell)
    assert ran == [Cell("fix", "t2", 1).id]  # only the crashed cell runs again
    assert recorded == [Cell("fix", "t2", 1)] and crashed == []
    assert len(store.done()) == 8
    assert store.rows()[0]["wall_s"] == 1.5


def test_a_failed_run_with_output_is_recorded(tmp_path: Path) -> None:
    store = ResultsStore(tmp_path)
    assert store.record(Cell("a", "t", 0), RunOutcome(exit_code=2, output="Traceback ..."))
    assert store.done() == {Cell("a", "t", 0).id}


def test_torn_last_line_means_the_cell_runs_again(tmp_path: Path) -> None:
    store = ResultsStore(tmp_path)
    store.record(Cell("a", "t", 0), RunOutcome(0, "ok"))
    with open(store.path, "a", encoding="utf-8") as handle:
        handle.write('{"cell": "half')
    assert store.done() == {Cell("a", "t", 0).id}


def test_arms_vary_exactly_one_thing() -> None:
    base = Arm("base", pythonpath="/trees/main", config={"agent.max_turns": 30})
    assert check_arms(base, Arm("fix", "/trees/fix", {"agent.max_turns": 30})) == "PYTHONPATH"
    assert check_arms(base, Arm("fix", "/trees/main", {"agent.max_turns": 40})) == (
        "config agent.max_turns"
    )
    with pytest.raises(ArmError, match="2 things"):
        check_arms(base, Arm("fix", "/trees/fix", {"agent.max_turns": 40}))
    with pytest.raises(ArmError, match="2 things"):
        check_arms(base, Arm("fix", "/trees/main", {"agent.max_turns": 31, "x.y": 1}))
    with pytest.raises(ArmError, match="do not differ"):
        check_arms(base, Arm("fix", "/trees/main", {"agent.max_turns": 30}))
    with pytest.raises(ArmError, match="named"):
        check_arms(base, Arm("base", "/trees/fix", {"agent.max_turns": 30}))


def test_arm_env_sets_pythonpath_only_when_given() -> None:
    assert Arm("a", "/t").env({"PATH": "/bin"}) == {"PATH": "/bin", "PYTHONPATH": "/t"}
    assert Arm("b").env({"PATH": "/bin"}) == {"PATH": "/bin"}


def test_oracles(tmp_path: Path) -> None:
    assert marker("VALUE=7")("so VALUE=7 here", tmp_path).ok
    assert not marker("VALUE=7")("VALUE=8", tmp_path).ok
    assert matches(r"SUM=(\d+)", expect="42")("the SUM=42.", tmp_path).ok
    assert not matches(r"SUM=(\d+)", expect="42")("SUM=41", tmp_path).ok
    assert not matches(r"SUM=\d+")("nothing", tmp_path).ok
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "v.txt").write_text("1.0,2.0", encoding="utf-8")
    assert file_check("out/v.txt", contains="2.0")("", tmp_path).ok
    assert not file_check("out/v.txt", contains="3.0")("", tmp_path).ok
    assert not file_check("out/none.txt")("", tmp_path).ok


# -- the store copy --------------------------------------------------------------------------------


def make_store(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
        "tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL, active INTEGER)"
    )
    call = lambda cid, name: {"id": cid, "type": "function", "function": {"name": name}}  # noqa: E731
    rows = [
        ("user", "go", None, None, None, 100.0),
        ("assistant", "", None, json.dumps([call("c1", "terminal")]), None, 101.0),
        ("tool", json.dumps({"output": "", "error": "boom"}), "c1", None, "terminal", 102.0),
        ("assistant", "", None, json.dumps([call("c2", "terminal")]), None, 103.0),
        ("tool", json.dumps({"output": "ok", "exit_code": 1}), "c2", None, "terminal", 104.0),
        ("assistant", "done", None, None, None, 110.5),
    ]
    for role, content, cid, calls, name, at in rows:
        connection.execute(
            "INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls, "
            "tool_name, timestamp, active) VALUES ('s1', ?, ?, ?, ?, ?, ?, 1)",
            (role, content, cid, calls, name, at),
        )
    connection.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, active) "
        "VALUES ('s1', 'assistant', 'rewound', 120.0, 0)"
    )
    connection.commit()
    connection.close()


def test_live_store_is_refused_and_a_copy_is_read(tmp_path: Path) -> None:
    live = tmp_path / "live-home" / "state.db"
    live.parent.mkdir()
    make_store(live)
    run_dir = tmp_path / "exp"
    run_dir.mkdir()
    with (
        pytest.raises(LiveStoreError, match="not under the run folder"),
        open_store_copy(live, run_dir=run_dir, live=live),
    ):
        pass
    linked = run_dir / "linked.db"
    os.link(live, linked)
    with (
        pytest.raises(LiveStoreError, match="live session store"),
        open_store_copy(linked, run_dir=run_dir, live=live),
    ):
        pass
    copy = run_dir / "copy.db"
    shutil.copy(live, copy)
    with open_store_copy(copy, run_dir=run_dir, live=live) as connection:
        rows = read_session_rows(connection, "s1")
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM messages")
    assert len(rows) == 6 and rows[1]["tool_calls"][0]["id"] == "c1"
    metrics = trace_metrics(rows)
    assert (metrics.model_calls, metrics.tool_calls) == (3, 2)
    assert metrics.tool_errors == 1  # exit code 1 is not an error
    assert metrics.retries == 1  # the second terminal call follows its failure
    assert metrics.wall_seconds == 10.5 and metrics.result_bytes > 0


# -- observed vs modelled --------------------------------------------------------------------------


def test_observed_and_modelled_are_never_summed() -> None:
    with pytest.raises(MixedLabels):
        total([Figure("tool_calls", 3, "observed"), Figure("tool_calls", 2, "modelled")])
    assert total([Figure("x", 1, "observed"), Figure("x", 2, "observed")]).value == 3
    with pytest.raises(ValueError):
        Figure("x", 1, "guessed")  # type: ignore[arg-type]
    report = Report()
    report.add(Figure("tool_calls", 3, "observed"), Figure("tool_calls", 4, "observed"))
    report.add(Figure("tool_calls", 10, "modelled"))
    assert report.totals("observed") == {"tool_calls": 7}
    assert report.totals("modelled") == {"tool_calls": 10}
    text = report.render()
    observed, modelled = text.split("modelled:")
    assert "7" in observed and "10" not in observed and "10" in modelled
