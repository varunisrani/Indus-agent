"""P24-12: worker lanes with real child processes.

The worker program is a short Python script of ours (not the agent): it reads the board pins the
dispatcher gives it and, depending on the task body, completes the task through the store with a
handoff, or exits 0 / 75 / 78 without a board call.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from indus.board.dispatcher import Dispatcher, ProcessSpawner, WorkerSpec
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope, make_scope
from indus.state.board import BoardStore, Task

WORKER = """
import os, sys
from pathlib import Path
from indus.foundation.scope import EnvView, make_scope
from indus.state.board import BoardStore

env = dict(os.environ)
task_id = env["INDUS_BOARD_TASK"]
run_id = int(env["INDUS_BOARD_RUN"])
root = Path(env["INDUS_BOARD_ROOT"])
home = Path(env["INDUS_HOME"])
scope = make_scope(home, env=EnvView(env))
store = BoardStore(scope, env["INDUS_BOARD_SLUG"], root=root)
mode = (store.require(task_id).body or "").strip()
if mode == "complete":
    store.complete(task_id, summary="wrote " + Path.cwd().name, expected_run_id=run_id)
    store.close()
    sys.exit(0)
store.close()
sys.exit(int(mode.split(":")[1]))
"""


class ScriptDispatcher(Dispatcher):
    """The real dispatcher with our worker script in place of `indus chat`."""

    def build_spec(self, task: Task, run_id: int, claimer: str) -> WorkerSpec:
        spec = super().build_spec(task, run_id, claimer)
        return replace(spec, argv=(sys.executable, "-c", WORKER))


@pytest.fixture
def real_scope(scope: Scope) -> Scope:
    return make_scope(scope.home, env=scope.env, clock=SystemClock())


@pytest.fixture
def lane(real_scope: Scope) -> Iterator[tuple[BoardStore, ScriptDispatcher, ProcessSpawner]]:
    store = BoardStore(real_scope)
    spawner = ProcessSpawner(wait_s=2.0)
    dispatcher = ScriptDispatcher(store, scope=real_scope, spawner=spawner, memory_mib=lambda: 4096)
    yield store, dispatcher, spawner
    spawner.stop_all()
    store.close()


def _wait_for_exit(spawner: ProcessSpawner, store: BoardStore, task_id: str) -> None:
    pid = store.require(task_id).worker_pid
    assert pid is not None
    assert spawner.wait(pid, 30.0) is not None, "the worker did not finish"


def test_a_worker_completes_its_task_with_a_handoff(
    lane: tuple[BoardStore, ScriptDispatcher, ProcessSpawner],
) -> None:
    store, dispatcher, spawner = lane
    task = store.create("write the report", assignee="work", body="complete")
    assert dispatcher.tick().spawned == [(task.id, "work")]
    _wait_for_exit(spawner, store, task.id)
    report = dispatcher.tick()
    assert report.violations == [] and report.crashed == []
    done = store.require(task.id)
    assert done.status == "done"
    run = store.latest_run(task.id)
    assert run is not None and run.summary == f"wrote {task.id}"
    log = store.logs_dir() / f"{task.id}.log"
    assert log.exists()


def test_exit_codes_of_real_workers(
    lane: tuple[BoardStore, ScriptDispatcher, ProcessSpawner],
) -> None:
    store, dispatcher, spawner = lane
    limited = store.create("rate limited", assignee="work", body="exit:75")
    refused = store.create("bad credential", assignee="work", body="exit:78")
    silent = store.create("silent", assignee="work", body="exit:0")
    report = dispatcher.tick()
    assert len(report.spawned) == 3
    for task in (limited, refused, silent):
        _wait_for_exit(spawner, store, task.id)
    report = dispatcher.tick()
    assert report.rate_limited == [limited.id]
    assert store.require(limited.id).consecutive_failures == 0
    assert store.lane_cooled_until("work") is not None
    assert store.require(refused.id).status == "blocked"
    assert store.require(refused.id).block_kind == "capability"
    assert report.violations == [silent.id]
    assert store.require(silent.id).protocol_violations == 1
    # The lane is cooling: nothing of `work` starts again yet.
    assert report.spawned == []
    assert Path(store.workspaces_dir() / limited.id).is_dir()
