# Task: pin policy guard and aggregate CI gate (P00-09) Phase: P00 Owner: builder (pins + locales)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `.github/workflows/ci.yaml` (only the
aggregate gate job: that it lists its needs, runs with `if: always()` and hands the needs results to
a script), `scripts/gen-bootstrap-pins.py` (size and purpose only). Closed before writing.
Evidence: R10.11, R10.12, R10.18, R10.22, R10.30 (research 10 §6), D-16.52.

What it does (own words): one written dependency policy, checked literally by
`python -m tooling.checks pins`, so the rule in the docs and the rule in CI cannot drift apart.
Python and Node dependencies are exact versions, every version is at least 14 days old when the
lock is made, git sources name a full commit, and nothing installs per interpreter version. The CI
workflow ends in one `gate` job that is green only when every other job ran and succeeded, so a
job that was skipped by a broken condition cannot hide behind a green summary. On pull requests
the guards read their policy files from the base branch, so a PR cannot loosen the rules it is
judged by.

Inputs → outputs:
- `pyproject.toml` (`[project].dependencies`, `[project.optional-dependencies]`,
  `[dependency-groups]`, `[tool.uv] exclude-newer`, `[tool.uv.sources]`), `uv.lock`
  (`upload-time` of every locked file), `tooling/checks/pins_allow.json`,
  `tooling/checks/pins_meta.json`, every `package.json` outside `node_modules`,
  `pnpm-workspace.yaml` → a `Result` with one violation line per problem:
  `<file>: <entry>: <what is wrong> -> <the fix>`.
- Exit 0 clean, 1 violation, 2 setup problem (unreadable policy file, allow entry without a reason
  or with an unbounded spec, malformed `pins_meta.json`).
- `python -m tooling.checks.policy --base <git ref>` copies every policy file from the base commit
  over the working tree (CI, pull requests only) and prints what it replaced.

Rules (each one a test):
1. Python entries: `name[extras]==X.Y[.Z]`. Extras allowed. A marker may only use `sys_platform`
   and `platform_machine` (no `python_version`, `python_full_version`, `implementation_name`, …).
   A bounded range `>=a,<b` is allowed only when `pins_allow.json` has an entry for that name with
   the same spec and a non-empty reason; an entry without a reason or with any other spec shape is
   a setup error (exit 2). An allow entry that no dependency uses is a warning.
2. `[tool.uv] exclude-newer` exists, is a full RFC 3339 timestamp (date, time, offset), and is at
   least 14 days before `pins_meta.json` `lock_date`; `lock_date` itself may not lie in the
   future. Extra (ACC-S8-1 "young versions"): every `upload-time` in `uv.lock` is at or before
   `exclude-newer`.
3. `package.json` (`dependencies`, `devDependencies`, `optionalDependencies`; `peerDependencies`
   exempt): exact `X.Y.Z` (pre-release suffix allowed, it is still exact); `workspace:*` only for
   `@indus/*`; anything else (`^ ~ * x latest >= ||`, tags, `npm:` aliases, file/link) fails.
4. Any `package.json` present → `pnpm-workspace.yaml` must exist with top-level
   `minimumReleaseAge` ≥ 20160 (minutes).
5. Git sources: `name @ git+URL@<rev>` in pyproject, `{ git = … }` in `[tool.uv.sources]`
   (`rev` must be 40 hex; `branch`/`tag` fail), `git+…#<rev>` / `github:…#<rev>` in package.json.

Files: `tooling/checks/pins.py`, `tooling/checks/pins_allow.json`, `tooling/checks/pins_meta.json`,
`tooling/checks/policy.py` (new, base-branch overlay), `tooling/checks/tests/test_pins.py`,
`tooling/checks/tests/test_policy.py`, `.github/workflows/ci.yml`, `pyproject.toml` (`[tool.uv]`
only), registration in `tooling/checks/__init__.py` (`NAMES`, shared).

Edge cases / failure modes:
- No pyproject, no package.json → nothing to check, a note, exit 0.
- `{include-group = …}` tables inside `[dependency-groups]` are not requirements; skipped.
- Names compared after PEP 503 normalisation (`Foo_Bar` = `foo-bar`).
- `uv.lock` missing while `exclude-newer` is set → note only (the lock job catches it).
- A policy file new in the PR (absent on base) keeps the PR's copy and says so: a brand-new guard
  has nothing on base to read; from the next PR on it is judged from base.
- A policy file deleted by the PR is restored from base.

Our design differences / decisions:
- One 14-day rule for both ecosystems, enforced exactly as written (the reference has a split
  window and a weak range check).
- No interpreter bridge markers at all (R10.12).
- The gate is a few lines of shell over `toJSON(needs)` with `jq`, no script and no release mode;
  every job in `needs` must be `success`.
- Policy-from-base is an explicit overlay step with a testable helper, not a per-check option, so
  every present and future guard is covered by one list of globs.

Dates (2026-10-18): `pins_meta.json` `lock_date` = `2026-10-18` (the day this lock is cut);
`exclude-newer` = `2026-10-04T00:00:00Z`, exactly 14 days earlier, the same cutoff as the pin table
in `00-layout-and-conventions.md` §4. pytest 9.1.1 (2026-06-19) and ruff 0.16.10 (2026-10-01) are
inside it; the transitive iniconfig 2.3.1 (2026-10-06) and pluggy 1.7.0 (2026-10-15) are not, so the
relock moves them back to their newest versions released before the cutoff.

Tests we will write:
- `test_pins.py`: synthetic repos in `tmp_path` for every rule (pass and fail), allow entry
  without reason → exit 2, real repo passes, CI shape (gate needs every other job, `if: always()`,
  fails on non-success; 3.14 lane present).
- `test_policy.py`: a git repo in `tmp_path` whose PR commit widens `pins_allow.json` and adds a
  range still fails after the base overlay (R10.22).

## Decisions for the lead
- Note names: the task brief says `pins-and-gate.md` / `locales.md`; the phase file lists
  `locales-parity.md` for P00-14. I used the brief's names.
- Version shape is literally `X.Y[.Z]`; a `.postN` or `rcN` pin would fail. If one is ever needed,
  widen the regex in `pins.py` with a reason.
- `[build-system].requires = ["hatchling"]` is not in the rule list and stays unpinned; suggest a
  later task pins it (and adds it to rule 1).
- Gate evaluation uses `jq` (preinstalled on GitHub runners) instead of a script file.
- New file `tooling/checks/policy.py` (+ its test) for R10.22.

Compare checklist (filled after the rewrite; reopened `.github/workflows/ci.yaml` gate job):
- [x] Gate runs after every job with `if: always()` and judges each result (test:
  `tooling/checks/tests/test_pins.py::test_ci_gate_needs_every_job`).
- [x] Skipped/cancelled count as failure for every job, not only in a release mode (test:
  `tooling/checks/tests/test_pins.py::test_ci_gate_fails_on_anything_but_success`).
- [x] One literal policy (ranges, markers, age window, git revs) (tests: `test_pins.py::*`).
- [x] Policy read from base on PRs (test: `tooling/checks/tests/test_policy.py::test_widened_allow_list_in_pr_still_fails`).
- Deliberate differences: the reference picks which lanes are required per change and has a
  separate strict release mode; we have no change classifier yet (P23), so every job is always
  selected and always required.
- Not taken: versioned gate check names, timing reports, PR comment pollers.

## Status

Done (2026-10-18). `tooling/checks/__init__.py` (`NAMES`) touched as shared; `uv.lock` regenerated
in the worktree only to test, the lead re-locks. Lead additions in the same slice: `banned` and
`similarity` also scan `packages/`; `licenses` reads root, `apps/*` and `packages/*` package.json
production deps (devDependencies noted, `workspace:` siblings skipped;
`tests/test_licenses_packages.py`); pyright fixes in `licenses.py`, `tests/test_licenses.py`,
`tests/test_similarity.py`; `ts` job uses `corepack enable` after setup-node.
