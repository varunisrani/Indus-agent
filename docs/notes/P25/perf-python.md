# Task: Performance budgets, Python (P25-12) Phase: 25 Owner: wave-41 builder G1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — our budgets; numbers from
`docs/plan/04-architecture.md` §8 (D-04.1, D-04.4) and the P04 trials.
Evidence: none.

What it does (own words): `tooling/perf/budgets.toml` holds every performance number (Python and
TypeScript). `tests/perf/test_budgets.py` measures the seven Python budgets on injected workloads
with no network — the fake model server, the fake summariser, temporary stores — five runs each,
and compares the median with the budget and with `tooling/perf/baseline.json` (a median more than
10 % over its recorded baseline fails). Each failure message starts with the budget name. The
perf tests carry `perf` and `slow`, so the existing CI step "Perf bounds (serial, alone)"
(`pytest -m perf`) runs them alone; the harness tests run in the ordinary lane.

| Budget | Workload | Limit | Here (median of 5) |
|---|---|---|---|
| `cli_version` | `python -m indus --version` in a fresh home | 150 ms | 45 ms |
| `oneshot_first_token` | `fake_runner -z "say hello"` to the first stdout byte | 1,500 ms | 1,313 ms |
| `fifty_sessions_p99` | D-04.1 (c) on the real loop and store, p99 per run | 50 ms | 7 ms |
| `interrupt_close` | D-04.1 (a) `chat-text`, worst of 5 interrupts per run | 200 ms | 0.1 ms |
| `store_append_10k` | 10,000 messages, 200 batches of 50, FTS on | 3,000 ms | 1,179 ms |
| `search_100k` | `state.search` over 100,000 messages | 300 ms | 104 ms |
| `compress_200_turns` | `BuiltinEngine.compress`, 200 turns, instant summariser | 2,000 ms | 252 ms |

Files: `tooling/perf/budgets.toml`, `tooling/perf/baseline.json`, `tests/perf/__init__.py`,
`tests/perf/conftest.py` (harness: `Budget`, `Meter`, `judge`, baseline recording),
`tests/perf/test_budgets.py`.

Baseline: `baseline.json` ships empty (only recorded names are compared). Record on the clean CI
runner with `INDUS_PERF_RECORD=<existing note path> uv run pytest -m perf tests/perf`; the run
writes the medians, the runner name, the date and the note path. Without a note it refuses.

Edge cases: the one-shot child is read unbuffered (a buffered first read hides the rest from
`communicate`); each D-04.1 and store run uses its own profile home so session ids never collide;
the 100,000-message store is built once per module.

Tests: every budget in the file; perf tests marked `perf` + `slow`; the TOML stays in the subset
the Node readers parse; a synthetic slowdown (a workload that moves a fake clock past the budget,
no real sleep) fails with "compress_200_turns: median … over the budget"; > 10 % over a baseline
fails and ≤ 10 % passes; recording keeps the note.

## Decisions for the lead
- **Store append budget is measured on turn-sized batches (50 rows per commit).** One commit per
  row takes ~2.9 ms here (28.7 s for 10,000): the bundled SQLite 3.45.1 makes the store fall back
  to DELETE journaling (the WAL-reset guard), so every commit rewrites the journal. Per-row appends
  would fail the 3 s budget on any runner with that SQLite. If the budget is meant per row, that
  is a finding against the store, not something a test change can fix.
- `interrupt_close` reuses P04's `interrupt_runs` (stream close time as P04 measures it).

## Results

`uv run pytest -m perf tests/perf`: 7 passed (19.7 s); harness tests: 6 passed.

## Status

Done (2026-10-21). Baseline to be recorded on the CI runner by the lead.
