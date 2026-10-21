"""P25-12: Python performance budgets.

The ``perf`` tests measure each budget of ``tooling/perf/budgets.toml`` (``[python.*]``) on
injected workloads with no network: the fake model, fake summariser and a temporary store. Run
them alone on a clean runner (``uv run pytest -m perf tests/perf``); ``-s`` prints ``PERF`` lines.
The harness tests at the bottom run in the ordinary lane: they pin the budget file and show that
a synthetic slowdown (an injected slow path on a fake clock, no real sleep) fails with the budget
name, and that a regression over the recorded baseline fails.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.core.compression.anchor import TokenMeter
from indus.core.compression.cooldown import CooldownBook, MemoryCounters
from indus.core.compression.engine import BuiltinEngine, CompressRequest
from indus.core.compression.threshold import CompressionSettings
from indus.core.exit_reasons import ExitReason
from indus.core.persist import StateStore
from indus.core.transcript import TranscriptRow
from indus.core.turn_state import TurnResult
from indus.foundation.scope import EnvView, Scope, make_scope
from indus.foundation.secrets import open_secret_store
from indus.state import messages, registry, search, sessions
from tests.fakes.aux_model import FakeAuxModel
from tests.fakes.clock import FakeClock
from tests.fakes.model import Finish, Stall, Text, Turn
from tests.integration.core import test_d04_1_criteria as d041
from tests.perf.conftest import BUDGETS, Budget, Meter, judge, load_budgets, record_baseline
from tests.unit.core.compression._transcripts import conversation

ROOT = Path(__file__).resolve().parents[2]
PYTHON_BUDGETS = {
    "cli_version",
    "oneshot_first_token",
    "fifty_sessions_p99",
    "interrupt_close",
    "store_append_10k",
    "search_100k",
    "compress_200_turns",
}


def fresh_scope(base: Path, clock: FakeClock) -> Scope:
    """A new profile home (own store file) on the shared fake clock."""
    home = base / "profiles" / "work"
    home.mkdir(parents=True, exist_ok=True)
    (home / "profile.yaml").touch()
    env = EnvView.from_process().with_overlay({"INDUS_HOME": str(home)})
    scope = make_scope(home, env=env, clock=clock)
    return scope.with_secrets(open_secret_store(home, scope.profile_id, mode="file"))


def child_env(home: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("INDUS_")}
    env.update({"INDUS_HOME": str(home), "PYTHONPATH": str(ROOT), **extra})
    return env


# -- CLI ------------------------------------------------------------------------------------------


@pytest.mark.perf
@pytest.mark.slow
def test_cli_version(perf_meter: Meter, tmp_path: Path) -> None:
    env = child_env(tmp_path / "home")

    def version() -> None:
        subprocess.run(
            [sys.executable, "-m", "indus", "--version"], env=env, capture_output=True, check=True
        )

    perf_meter.check("cli_version", version)


@pytest.mark.perf
@pytest.mark.slow
def test_oneshot_first_token(perf_meter: Meter, tmp_path: Path) -> None:
    runs = iter(range(1000))

    def first_token() -> float:
        base = tmp_path / f"run{next(runs)}"
        (base / "home").mkdir(parents=True)
        (base / "work").mkdir()
        env = child_env(base / "home", INDUS_TEST_MODEL_SCRIPT="hello")
        started = time.perf_counter()
        child = subprocess.Popen(
            [sys.executable, "-m", "tests.integration.cli.fake_runner", "-z", "say hello"],
            cwd=base / "work",
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,  # unbuffered: the first byte is read as soon as it is written
        )
        assert child.stdout is not None
        first = child.stdout.read(1)
        elapsed = (time.perf_counter() - started) * 1000.0
        out, err = child.communicate(timeout=60)
        assert child.returncode == 0, err[-500:]
        assert first + out == b"hello\n"
        return elapsed

    perf_meter.check("oneshot_first_token", first_token)


# -- D-04.1 (a) and (c) on the real turn loop ------------------------------------------------------


@pytest.mark.perf
@pytest.mark.slow
def test_interrupt_close(perf_meter: Meter, tmp_path: Path, fake_clock: FakeClock) -> None:
    runs = iter(range(1000))

    def worst_close() -> float:
        scope = fresh_scope(tmp_path / f"run{next(runs)}", fake_clock)
        with registry.open_unshared(scope) as handle:
            close_ms, _ = asyncio.run(
                d041.interrupt_runs(scope, fake_clock, handle, "chat-text", runs=5)
            )
        return max(close_ms)

    perf_meter.check("interrupt_close", worst_close)


SESSIONS = 50
DELTAS = 200


async def fifty_sessions(scope: Scope, clock: FakeClock, handle: registry.StoreHandle) -> float:
    """One run of D-04.1 (c): 50 sessions released together; the p99 frame-to-sink latency."""
    store = StateStore(handle)
    latencies = [d041.DeltaLatency() for _ in range(SESSIONS)]
    kits = [
        d041.make_kit(
            scope,
            clock,
            [Turn((Stall(1.0), Text(d041.tagged(DELTAS)), Finish("stop")))],
            session_id=f"perf-{n:02d}",
            store=store,
            latency=latencies[n],
        )
        for n in range(SESSIONS)
    ]

    async def one(n: int) -> TurnResult:
        mode = d041.CHAT if n % 2 == 0 else d041.ANTHROPIC
        return await d041.run_once(
            kits[n], "stream to me", api_mode=mode, on_event=lambda e, _s: latencies[n].on_event(e)
        )

    async def release_together() -> None:
        while sum(len(k.server.streams) for k in kits) < SESSIONS:
            await d041.REAL.sleep(0.001)
        for _ in range(60):
            if all(lat.sent or lat.ms for lat in latencies):
                return
            clock.advance(1.0)
            await d041.REAL.sleep(0.002)
        raise AssertionError("the start barrier never released every stream")

    barrier = asyncio.ensure_future(release_together())
    results = await asyncio.gather(*(one(n) for n in range(SESSIONS)))
    await barrier
    assert all(r.exit_reason is ExitReason.COMPLETED for r in results)
    all_ms = [ms for lat in latencies for ms in lat.ms]
    assert len(all_ms) == SESSIONS * DELTAS
    return d041.p99(all_ms)


@pytest.mark.perf
@pytest.mark.slow
def test_fifty_sessions_p99(perf_meter: Meter, tmp_path: Path, fake_clock: FakeClock) -> None:
    runs = iter(range(1000))

    def p99_of_a_run() -> float:
        scope = fresh_scope(tmp_path / f"run{next(runs)}", fake_clock)
        with registry.open_unshared(scope) as handle:
            return asyncio.run(fifty_sessions(scope, fake_clock, handle))

    perf_meter.check("fifty_sessions_p99", p99_of_a_run)


# -- store ----------------------------------------------------------------------------------------

WORDS = ("alpha", "beta", "gamma", "delta", "river", "stone", "cloud", "ember")


def message(i: int, tag: str) -> dict[str, str]:
    text = f"{WORDS[i % 8]} {WORDS[(i * 3) % 8]} note {tag}-{i} about the build"
    return {"role": "user" if i % 2 == 0 else "assistant", "content": text}


@pytest.mark.perf
@pytest.mark.slow
def test_store_append_10k(perf_meter: Meter, tmp_path: Path, fake_clock: FakeClock) -> None:
    runs = iter(range(1000))

    def append_10k() -> float:
        scope = fresh_scope(tmp_path / f"run{next(runs)}", fake_clock)
        with registry.open_unshared(scope) as handle:
            sessions.create(handle, "s1", "cli")
            batches = [[message(b * 50 + i, "a") for i in range(50)] for b in range(200)]
            started = time.perf_counter()
            for batch in batches:
                messages.append_batch(handle, "s1", batch)
            elapsed = (time.perf_counter() - started) * 1000.0
            assert search.search(handle, "note a-9999", limit=1)  # the FTS index has the rows
        return elapsed

    perf_meter.check("store_append_10k", append_10k)


@pytest.fixture(scope="module")
def hundred_k(tmp_path_factory: pytest.TempPathFactory) -> Iterator[registry.StoreHandle]:
    from tests.conftest import FAKE_CLOCK_START

    scope = fresh_scope(tmp_path_factory.mktemp("search100k"), FakeClock(FAKE_CLOCK_START))
    with registry.open_unshared(scope) as handle:
        for s in range(10):
            sessions.create(handle, f"b{s}", "cli")
            for b in range(10):
                batch = [message(b * 1000 + i, f"{s}") for i in range(1000)]
                messages.append_batch(handle, f"b{s}", batch)
        yield handle


@pytest.mark.perf
@pytest.mark.slow
def test_search_100k(perf_meter: Meter, hundred_k: registry.StoreHandle) -> None:
    queries = iter(["river stone", "ember note", "cloud build", "gamma", "note 7-4242"] * 10)

    def one_search() -> None:
        assert search.search(hundred_k, next(queries), limit=20)

    perf_meter.check("search_100k", one_search)


# -- compression ----------------------------------------------------------------------------------


def engine(clock: FakeClock) -> BuiltinEngine:
    settings = CompressionSettings()
    return BuiltinEngine(
        settings=settings,
        meter=TokenMeter(),
        book=CooldownBook(
            MemoryCounters(),
            clock,
            timeout_s=settings.timeout_s,
            abort_on_failure=settings.abort_on_summary_failure,
        ),
        clock=clock,
        summarizer=FakeAuxModel(clock=clock),  # answers at once: the model is excluded
        window=200_000,
        model="perf-model",
    )


async def keep(new: list[TranscriptRow], held: list[TranscriptRow]) -> list[TranscriptRow]:
    return [r.clone() for r in new]


@pytest.mark.perf
@pytest.mark.slow
def test_compress_200_turns(perf_meter: Meter, fake_clock: FakeClock) -> None:
    rows = conversation(random.Random(7), 200, big=2_000)

    def compress() -> None:
        out = asyncio.run(engine(fake_clock).compress(rows, CompressRequest(gate="manual"), keep))
        assert out.status == "compressed" and out.condensed > 0

    perf_meter.check("compress_200_turns", compress)


# -- the harness itself (ordinary lane) -----------------------------------------------------------


def test_the_budget_file_holds_every_python_budget_of_the_row() -> None:
    budgets = load_budgets()
    assert set(budgets) == PYTHON_BUDGETS
    assert {b.runs for b in budgets.values()} == {5}
    assert {b.metric for b in budgets.values()} == {"median"}
    assert all(b.regression == pytest.approx(0.10) for b in budgets.values())


def test_every_perf_test_is_marked_perf_and_slow() -> None:
    names = {f"test_{n}" for n in PYTHON_BUDGETS}
    module = sys.modules[__name__]
    for name in names:
        marks = {m.name for m in getattr(module, name).pytestmark}
        assert {"perf", "slow"} <= marks, name


def test_the_budget_file_stays_in_the_subset_the_node_readers_parse() -> None:
    line = re.compile(
        r'^(?:#.*|\[[a-z_]+(?:\.[a-z0-9_]+)?\]|[a-z_0-9]+ = (?:-?[0-9.]+|"[^"\\]*"))?$'
    )
    bad = [ln for ln in BUDGETS.read_text(encoding="utf-8").splitlines() if not line.match(ln)]
    assert bad == []


def test_a_synthetic_slowdown_fails_with_the_budget_name(fake_clock: FakeClock) -> None:
    budgets = load_budgets()
    meter = Meter(budgets, {}, clock=fake_clock.monotonic)
    limit_s = budgets["compress_200_turns"].limit_ms / 1000.0

    def fast_path() -> None:
        fake_clock.advance(limit_s / 4)

    def slow_path() -> None:  # the injected slow path: the fake clock moves past the budget
        fake_clock.advance(limit_s * 1.5)

    meter.check("compress_200_turns", fast_path)
    with pytest.raises(AssertionError) as failed:
        meter.check("compress_200_turns", slow_path)
    assert str(failed.value).startswith("compress_200_turns: median 3000.0 ms is over the budget")


def test_a_regression_over_the_baseline_fails_and_ten_percent_passes() -> None:
    budget = Budget("search_100k", 300.0, "median", 5, 0.10, "test")
    assert judge(budget, [109.0] * 5, {"search_100k": 100.0}) == []
    problems = judge(budget, [111.0, 112.0, 111.0, 90.0, 300.0], {"search_100k": 100.0})
    assert len(problems) == 1
    assert problems[0].startswith("search_100k: median 111.0 ms is more than 10% over")
    assert judge(budget, [111.0] * 5, {}) == []  # nothing recorded: only the budget applies


def test_recording_a_baseline_keeps_the_note(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"note": "x", "budgets": {"search_100k": 120.0}}), "utf-8")
    record_baseline({"cli_version": 61.23}, Path("docs/notes/P25/perf-python.md"), path)
    data = json.loads(path.read_text("utf-8"))
    assert data["budgets"] == {"cli_version": 61.2, "search_100k": 120.0}
    assert data["record_note"] == "docs/notes/P25/perf-python.md" and data["recorded"]
