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

## Wave 48 (builder e, 2026-10-22): start-up cost on the one-shot path

Status: partial (2026-10-22), not yet landed. Three real cuts are in; `oneshot_first_token` is
still over its 1,500 ms budget on this (shared, loaded) box. The budget is unchanged.

Read: no reference file (our own start-up path). Evidence: none.

Profile (`python -X importtime`, cProfile and wall-clock marks on `fake_runner -z "say hello"`,
main `1b6e711`, about 1.9 s to the first byte on a quiet moment): about 1.4 s is imports before
the turn, 0.25 s the keychain probe (the `keyring` library finding its backend), 0.1 s
`build_service`, 0.18 s the turn itself. Pydantic class construction dominates the imports:
`foundation.config.schema` (~150 ms; `DEFAULTS` is built from `ConfigModel()` at import, so it
is needed work) and the ~240 wire models of `indus.protocol` (~400 ms, of which only 6 are used
by a one-shot run).

Cuts (decisions for the lead):
- **W48-e-1 (D-16.1084)** `ProtocolModel` sets `defer_build=True`: each wire model builds its validator and
  serializer on first use. Fields, `model_fields`, docstring descriptions and the generated schema
  are unchanged (`indus.protocol.codegen --check` clean). Import of `protocol.events` + `methods`
  falls from ~390 ms to ~260 ms here.
- **W48-e-2 (D-16.1085)** `core.subagents.output_schema` imports `jsonschema` on first use (~50 ms; only a
  `delegate_task` with `output_schema` needs it).
- **W48-e-3 (D-16.1086)** `secrets.start_backend_discovery()`: a real CLI process (`entry.main` with no argv
  or env) starts `keyring.get_keyring()` in a daemon thread before its own imports; the probe's
  `_system_keyring` joins it, then asks as before (the probe deadline still bounds the caller).
  Discovery reads and writes no entry, but it now also runs for profiles with
  `security.secret_store: file` (the module docstring says so). Measured alone: 2,343 → 2,215 ms
  median (9 + 9, alternating).

Measurements (same box, same venv, `git archive` of main vs this tree, alternating, 9 samples
each, the perf test's own workload; load average 7.5–8.7 from the parallel builders): first byte
median base 2,532 ms → new 2,281 ms; process CPU to the first byte median 2,138 → 2,061 ms.
`pytest -m perf tests/perf/test_budgets.py::test_oneshot_first_token` afterwards (load ~5):
median 1,917 ms > 1,500 — still failing here.

Tests (fail-first: all five fail on main, pass here): `tests/unit/cli/test_oneshot_startup.py`
(no wire validator built by the one-shot imports; `jsonschema` not loaded),
`tests/unit/protocol/test_deferred_build.py` (deferred model still refuses unknown keys, stays
frozen, keeps docstring descriptions), `tests/unit/foundation/test_secret_probe.py`
(`test_backend_discovery_starts_once_and_the_probe_waits_for_it`,
`test_a_failed_discovery_leaves_the_answer_to_the_probe`).

Follow-ups (the rest of the gap, roughly in order of size):
- the pricing table (`providers/pricing.load_table`, 8 KB YAML) is parsed with the pure-Python
  ruamel loader inside the turn, before the reply: ~56 ms; skill front matter adds ~25 ms (15
  files). A generated JSON twin with a freshness check, or a cache, would remove most of it;
- `use_attribute_docstrings` on `ProtocolModel` reads and parses class source at import (~100 ms
  under the profiler); descriptions are only needed for schema generation;
- `httpx` imports its CLI (`httpx._main`: rich, pygments, click) at package import, ~80 ms, not
  ours to change;
- eager imports under `cli.runtime` that a plain one-shot never calls: docker backends, the
  browser and code-kernel tools, `cron.models`, `safety.content_scan` regexes (~20–30 ms each);
- judge the budget on a quiet runner (RM25); this box is shared by six builders.

## Wave 48 (builder g, 2026-10-22): one-shot start-up, continued

Status: partial (2026-10-22), not yet landed; continues builder e (apply w48e, then w48g). Four
more cuts are in; `oneshot_first_token` is still over its 1,500 ms budget on this box. The budget
is unchanged.

Read: no reference file (our own start-up path). Evidence: none.

Profile (wall-clock marks, `-X importtime`, cProfile cut at the first stdout byte, and a
profiler hook listing modules imported but never called before `os._exit`): on this box a quiet
run is ~1.8 s to the first byte; ~0.07 s interpreter, ~1.3 s imports, ~0.2 s `entry.main` up to
the store, ~0.4 s the turn (`build_service` ~0.12 s, skills index ~0.05 s, pricing ~0.05 s).

Cuts (decisions for the lead):
- **W48-g-1 (D-16.1090)** pricing: `providers/pricing_table.json` is a generated twin of the YAML (the YAML
  stays the source), holding the parsed data and the YAML's sha256. `default_table()` uses it
  only while the digest matches; a missing or stale twin falls back to parsing the YAML, so a
  stale twin costs time, never a wrong price. Regenerate: `uv run python -m
  indus.providers.pricing_twin` (`--check` exits 1 when stale; a unit test runs it). Table load
  ~45 ms → ~5 ms.
- **W48-g-2 (D-16.1091)** wire-model field docstrings: `ProtocolModel` no longer sets
  `use_attribute_docstrings` (pydantic re-reads and re-parses each class's source: 239
  `ast.parse` calls, ~55 ms at import). `__pydantic_init_subclass__` sets the same descriptions
  (same rule, only where a field has none) from `protocol/field_docs.json`, generated by `uv run
  python -m indus.protocol.field_docs` for every module under `src/indus` that names
  `ProtocolModel` (plus all of `indus.protocol`), with each module's sha256; a module that is not
  in the twin or has changed since is parsed once (not once per class). Descriptions are set
  before any validator is built (models are deferred, W48-e-1). Checked: the descriptions and
  `model_json_schema()` of all 238 wire models are byte-identical before and after, and
  `python -m indus.protocol.codegen --check` stays clean. `protocol.events` + `methods` import
  ~189 → ~137 ms (alternating, 9 + 9).
- **W48-g-3 (D-16.1092)** lazy imports a plain one-shot never calls: `backends/docker/liveness.py` holds the
  orphan sweep's liveness answers (the reaper reads them there; `reaper.set_liveness` is the same
  function re-exported), so `service.terminal_host` no longer imports the Docker backend;
  `label_value` is imported inside `jobs_alive`; the in-process host imports `CronStore` and
  `cron_refs` in `start()` (one-shot builds no such host); `project_trust` imports
  `safety.content_scan` (~27 ms of regex compiles) inside `scan_project_skill`. Together ~70 ms of
  imports.
- Not done here: code kernel and browser modules (`service.code_host`) are imported by
  `load_builtins` for tool registration anyway, so a lazy import there saves nothing.

Measurements (same box, same venv, `git archive` of 34c34d1 = main + w48e vs this tree,
alternating, 9 samples each, the perf test's own workload, first stdout byte; other builders'
tests running, load 1.9–2.4): base 2,086 / 2,115 / 1,905 ms → this tree 1,955 / 2,034 / 1,768 ms
(three rounds; ~80–140 ms lower; best samples 1,904 → 1,653 ms). `pytest -m perf -k
oneshot_first_token` afterwards (load ~2.3): median 1,975 ms > 1,500 — still failing here.
Pieces measured alone: pricing table 45 → 5 ms; wire-model import 189 → 137 ms; the three lazy
groups ~70 ms of `-X importtime`.

Tests (fail-first: each fails on 34c34d1, passes here): `tests/unit/providers/test_pricing_twin.py`
(twin fresh; same data as the YAML; the default table parses no YAML; a stale or broken twin falls
back), `tests/unit/protocol/test_field_docs.py` (twin fresh; importing the wire models calls
`ast.parse` 0 times, 239 on the base; a module missing from or changed since the twin is
parsed), `tests/unit/cli/test_oneshot_startup.py::test_the_one_shot_imports_leave_docker_cron_and_the_content_scanner_unloaded`.
`tests/unit/service/test_terminal_host.py` reads the liveness answers from the new module. and
`tests/unit/learning/skills/test_project_trust.py` patches `content_scan.scan_tree` (the scanner
is imported on first scan).

Results: pytest (not perf) protocol + foundation + providers + cli 3,627 passed; core + tools
2,378 passed; service + learning + backends/docker + cron + contract + security 3,925 passed
(after the project_trust test fix); ruff, pyright (touched files), lint-imports, security_static,
`protocol.codegen --check`, `gen_reference --check`, `tooling.checks --require-ref` ok
(deps_audit warns: pypdf, katex, sprintf-js; known).

Follow-ups (the rest of the gap, roughly in order of size):
- `foundation.config.schema` import ~190 ms: with `defer_build=True` on `_Section`/`ConfigModel`
  the import is ~90 ms, but `DEFAULTS` (`ConfigModel().model_dump()`) and `loader.load`'s
  `schema.validate` build every model at once. A design that keeps behaviour: a generated
  defaults twin (digest of `schema.py` + `versions.py`, test: twin == `_build_defaults()`), and
  validate only the sections that differ from the defaults (defaults are valid by construction;
  pin `validate(DEFAULTS) == []`). ~100 ms.
- `httpx` imports its CLI (`httpx._main`: rich, click, pygments) at package import, ~75 ms; not
  ours to change.
- skill front matter: 15 shipped skills parsed with ruamel per run (~25–50 ms); a generated
  front-matter twin for the shipped skills with the same digest fallback would remove it.
- 605 dataclasses and ~340 pydantic classes are built at import; what remains is mostly needed
  by the turn loop.
- judge the budget on a quiet runner (RM25): the P25-12 note measured 1,313 ms on its box; this
  box runs the same base at ~1.8 s with load near 0.

Integration (wave 48, D-16.1099): both twins are in the gate. `tooling/checks/generated_twins.py`
runs `python -m indus.providers.pricing_twin --check` and `python -m indus.protocol.field_docs
--check` against the checked root (`PYTHONPATH=<root>/src`, as `protocol_stale` does), so
`tooling.checks` (CI and the release `cleanroom` gate) fails on a stale twin; tested by
`tooling/checks/tests/test_generated_twins.py` (fresh repo, stale pricing twin, stale field-docs
twin, broken generator, root without generators). `field_docs.json` was regenerated at the
integration: w48h's wire-model changes (applied after w48g) had made it stale, which only costs
time (those modules fall back to one parse each), never a wrong description.
