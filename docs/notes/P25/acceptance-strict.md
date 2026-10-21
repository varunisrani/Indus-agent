# Task: Acceptance map strict (P25-10) Phase: 25 Owner: wave-41 builder G1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — a guard over our own map and
tests; the process source is the skill's planning reference §10 (ledger rules), not a reference
product file.
Evidence: none.

What it does (own words): `python -m tooling.checks acceptance --strict` is the v1.0 gate for the
acceptance map. Non-strict mode stays the everyday check (parses files, never runs tests). Strict
mode adds what a release needs to be true:

- every coverage line is a test reference (a `Review:` row fails, as before);
- every referenced test is really collected and not skipped: Python references are resolved
  against `pytest --collect-only` output over the cited files (a small collection plugin also
  records which items carry a skip mark whose condition is true here, and which modules skipped
  at import); TypeScript references (`file::title` or `file > describe > title`) are resolved
  against a scan of the spec file's `describe`/`it`/`test` calls, where `.skip`, `.todo`, `x…` and
  a skipped enclosing `describe` count as skipped;
- every ACC id names a feature that exists in the inventory (renamed ids fail; non-strict only
  warns);
- a `## Known gaps` section lists rows the 1.0 build leaves out: `- **<feature>** <what> (D-16.nn)`;
  each must name an inventory feature and cite a D-id that exists in §2.8 of
  `16-what-we-change.md`; a malformed gap row fails in both modes.

Inputs → outputs: the map, the inventory, `16-what-we-change.md` §2.8, the test files →
violations naming the row, the line and why.

Files: `tooling/checks/acceptance.py`, `tooling/checks/ledger.py` (` > ` references),
`tooling/checks/collect.py` (new: collected-test lookups shared with the traps check),
`tooling/checks/collect_plugin.py` (new: pytest plugin used by the collector),
`tooling/checks/tests/test_acceptance_strict.py`, `docs/internals/acceptance-map.md` (rows
resolved with existing tests; Known gaps).

Edge cases / failure modes:
- parametrized tests: `path::test` matches any `path::test[...]` item; `path::test[x]` must match
  exactly;
- a module skipped at import (`importorskip`, `pytest.skip(allow_module_level=True)`) makes every
  reference into it "skipped", with the reason;
- a test file that fails to import is reported as a collection error for each reference into it;
- `skipif` evaluated in the collecting environment: an environment-dependent skip fails strict on a
  runner that lacks the dependency — that is the point of the gate (run it where the lane runs);
- a TS title built from a template with `${…}` cannot be matched statically: the reference must
  quote the literal title.

Our design differences / decisions:
- strict collection runs pytest in a subprocess (no import side effects in the checks process);
  a file named on the command line is collected even when its name does not match
  `python_files`, so the collector also records the patterns and refuses such references;
- TS references use a static scan, not `vitest list`: `vitest list --json` prints names but not the
  skip mode, and needs an installed workspace; the scan sees `.skip`/`.todo`/`.fixme`/`x…`.
  Template-literal titles match with each `${…}` hole as a wildcard. Conditional skips
  (`.skipIf`/`.runIf`) are not evaluated statically: they count as runnable (their lane runs them).

Tests we will write:
- seeded Review row fails strict; seeded skipped test (mark, `skipif(True)`, module-level skip)
  fails strict; a test that does not exist in collection fails; renamed feature id fails strict and
  warns non-strict; TS: skipped/describe-skipped/missing titles fail, present ones pass; Known gaps
  rows validated (unknown feature, missing or non-§2.8 D-id); the real map in non-strict mode.

## Decisions for the lead
- `vitest list` / `playwright test --list` are not used for TS references (reason above); the
  static scan covers `it/test/describe/bench` with `.skip/.todo/.fixme/.only/.concurrent/.each` and
  `xit/xdescribe/xtest`.
- Known gaps rows live in the map under `## Known gaps`; a P0/P1 feature listed there with a valid
  §2.8 D-id counts as mapped (its built part may still have ACC rows).

Compare checklist (filled after the rewrite):
- [x] no reference product code for this task (nothing to compare)

## Map rows (this task)

- 69 rows swapped from `Review:` to existing tests (every "not yet planned (phase TBD)" row whose
  feature is built: A1–A34, A52, A64, B4–B19, C1–C41, D2–D14, E1–E13, J1–J17, L1–L20, S14, S17,
  Q5, Q9), plus A5 (persona loading, P04) and A55 (P08 transports) whose named review landed.
  No test was written for this: each reference is an existing test that pins the behaviour.
- `## Known gaps` added with §2.8 rows: Q9 (D-16.51), K12 (D-16.86, D-16.87), G19 (D-16.45), I46
  (D-16.50), I48 (D-16.49), N30 (D-16.48, D-16.15), N33 (D-16.47), S5, S6 (D-16.15), O9 (D-16.85).
- Still `Review:` (59 lines on 58 rows; `--strict` fails on each — owner action, not this task):
  - live lanes against real services, owner-run: D4-1, D5-2, K7-1, K8-1, K9-1, K9-2, K10-1, K10-4,
    K11-1…K11-6, K12-1, K12-2, K12-3, K12-4, K12-6, K12-9, K12-10, K12-11, K45-1, K46-1, S1-1,
    S2-1, S3-3 (P16-36, P15-36, P21-16, P11-21, P23-06/11/23, owner);
  - recorded live captures: A44-1, A45-1 (P03-11), A46-1 (P08-09);
  - parts not built yet, owner task named in the row: A17-1, A53-1, A68-1, B9-1, B10-1, B12-1,
    B16-1, B26-1, C7-1, C12-2, C14-1 (P24), E6-1, E18-1, G14-1, G19-1, I4-1, I6-1, I7-1 (P24
    board worker), I51-1, I54-1, K28-1, K28-2 (P16-33), M1-1, S8-1;
  - not built, no task: A22-1 (plain-language failure message), S11-1 (CI change classifier),
    S13-1 (per-file test runner); S18-1 is P25-17 (slice D1).
- `--strict` today also reports five references skipped in this container only (browser extra,
  `mautrix`, `pnpm install`): they run on a lane with `uv sync --all-extras` and an installed
  workspace, which is where the gate must run.

## Results

`python -m tooling.checks acceptance` ok (0 issues). `acceptance --strict`: 63 issues = 58 Review
rows above + 5 environment skips; every reference added here resolves to a collected, runnable
test. `tooling/checks/tests/test_acceptance_strict.py`: 31 passed (with `test_acceptance.py`).

## Status

Done (2026-10-21) for the tooling; the map passes `--strict` once the owner rows above land.
