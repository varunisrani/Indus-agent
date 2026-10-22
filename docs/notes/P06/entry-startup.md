# Task: Entry point and startup order (P06-01) Phase: 06 Owner: P06 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/main.py` (`main`,
`_apply_profile_override`, `_exit_after_oneshot`, `_cleanup_oneshot_runtime`; updater re-exports
skipped), `hermes_cli/_startup_fast.py` (version printer, install-method stamp probe), research
`04-cli-config.md` §3.1 and traps R04.2, R04.13, R04.14. Closed before writing.
Evidence: none.

What it does (own words): `indus` (console script `indus.cli.entry:main`, also `python -m indus`)
prepares the process in a fixed order before any module that depends on the profile is imported,
then hands argv to the click app. Order: (0) `-V`/`--version` alone (profile tokens ignored) is
answered at once from stdlib-only code; (1) provider key names are registered as managed `.env`
names so a key removed from `.env` is cleared; (2) `--yolo` found by a flag-aware scan of argv is
written as `INDUS_YOLO_MODE=1` into the env the scope is built from; (3) `foundation.bootstrap.run`
does profile pre-parse (value-flag aware, using the app's derived value-flag set), `.env` load with
the home's file winning, the `security.redact_secrets` bridge, then logging in `cli` mode (`gui`
for dashboard/serve/gui/desktop); (4) the scope's env view is published to the process env (single
profile CLI) and `--yolo` is laid over the view again so a `.env` line cannot undo the flag; the
provider credential names are registered with `safety.child_env` as blocked; (5) the lazy click
app runs; an int return is the exit code. A named profile that does not exist prints one error
line and exits 1; any other failure inside profile selection is already turned into a warning by
`bootstrap`/`preparse` and the run continues on the default profile.

Inputs → outputs: argv, process env → exit code; stdout for `--version` (version, install method,
install dir, Python).

Files: `src/indus/cli/__init__.py`, `src/indus/cli/entry.py`, `src/indus/cli/version.py`,
`tests/unit/cli/test_entry.py`; delete `src/indus/cli.py`; `src/indus/__main__.py`;
`pyproject.toml` (script, click/prompt_toolkit/rich pins, dev pexpect).

Edge cases / failure modes:
- `-p work --version` still takes the fast path (profile tokens stripped first); `--version chat`
  goes through the full path (eager option).
- `-z "--yolo"` is a prompt, not the flag: the scan skips values of value-taking flags; nothing
  after `--` is scanned.
- `.env` holding `INDUS_YOLO_MODE=0` cannot cancel an explicit `--yolo` (overlay after bootstrap).
- Version must stay under 150 ms: `entry` imports only stdlib + `indus.cli.version` at module load.
- Tests call `main(argv, env=dict)` so nothing leaks into the test process; the process-only
  steps (publish to `os.environ`, hard exit) run only when `env` is the process env.

Our design differences / decisions:
- Install method order: an `.install_method` stamp next to the package wins, then `nix` (path
  under `/nix/store`), `docker` (container markers), `git` (source tree with `.git`), `dev` (source
  tree without `.git`), `package` (installed wheel). No update check yet (P23).
- No process title, PID-1 warning, trust-store install, bytecode sweep, container exec or plugin
  discovery yet: those belong to later phases (P07/P13/P23).
- The yolo bridge happens before bootstrap (env) and again after (scope overlay); our approval code
  reads the scope per call, so the "frozen at import" hazard is pinned by an import-order spy.

Tests we will write:
- `-p work` anywhere is resolved and stripped; `--reasoning high chat` keeps `high` as a value.
- missing named profile → exit 1 with one line on stderr.
- yolo env present when `indus.safety.approvals` is first imported (subprocess spy).
- version fast path: output shape, no heavy modules imported, import time under budget.

## Decisions for the lead
- `prompt_toolkit==3.0.53` (2026-07-26, BSD-3-Clause), `click==8.5.0`, `rich==15.0.0`,
  `pexpect==4.9.0` (dev) were not in 00-layout §4; added there with this slice.

Compare checklist (filled after the rewrite; reopened `hermes_cli/main.py`, `_startup_fast.py`):
- [x] profile before anything else, flag stripped, value flags skipped (test:
  `tests/unit/cli/test_entry.py::test_profile_flag_anywhere_is_resolved_and_stripped`)
- [x] missing named profile exits 1 (test: `test_missing_named_profile_exits_1`)
- [x] yolo set before tool/approval import (test: `test_yolo_env_set_before_approval_import`)
- [x] redaction bridge before logging: done inside `bootstrap.run` (P01 tests pin it)
- [x] version fast path only for a bare `-V`/`--version` (test: `test_version_fast_path_*`)
- [x] int return becomes the exit code; one-shot hard exit after cleanups (P06-12)
- Deliberate differences: no update status line in `--version` (P23 adds it); a sticky profile
  that is missing exits 1 except for recovery commands (P01 behaviour, unchanged).
- Not taken: Windows launcher re-staging, `pm` short-circuit, Termux fast paths, container exec.

## Status

Done (2026-10-18). Also touched (shared): `src/indus/__main__.py`, `pyproject.toml`,
`docs/plan/phases/00-layout-and-conventions.md` §4 rows.

## Integration (lead)

- Pins merged from slices A and B (one row each in 00-layout §4); `uv.lock` regenerated.
  THIRD_PARTY_NOTICES rows: click, prompt_toolkit, rich (+ wcwidth, markdown-it-py, mdurl,
  Pygments), pexpect (dev), and the missing `web` extra rows ddgs and primp (lxml and ptyprocess
  rows note their extra users); licences from installed metadata.

## Wave 46 (builder k, 2026-10-22): F-4 `--version` on a source install

Read: no reference file (our release gap F-4). Files: `src/indus/cli/version.py`
(`head_commit`, `checkout_release`, `version_lines`), `scripts/install/sh/20-runtime.sh`
(`.indus-bootstrap.json` gains `version`), tests `tests/unit/cli/test_version_release.py`.

A git checkout whose HEAD is a release prints that release on the first line instead of `0.0.0`.
File probes only (the fast path's budget holds; `zlib` and `indus.foundation.versions` are imported
lazily, only for a checkout): HEAD from `.git` (a linked worktree's `gitdir`/`commondir` followed),
release tags from `packed-refs` (peeled lines) and loose `refs/tags/v*` (lightweight, or an
annotated tag whose object is loose, peeled with `zlib`); then the installer marker's
`version`+`commit` and the newest 20 update receipts' `to_version`+`to_commit`. Every candidate must
name the HEAD commit; the newest version wins; malformed tags are ignored. A HEAD past or off a
release keeps `0.0.0`. `indus.__version__` itself is unchanged (daemon/server `code_version`
comparisons keep using it).

Decisions for the lead:
- W46-k-4 (D-16.1019): only `--version`'s display changes; `__version__` (code_version, User-Agent, ACP
  agentInfo) stays the package's own. Owner may want those to follow; record if so.
Follow-up: `install.ps1`'s marker should gain `version` the same way.
