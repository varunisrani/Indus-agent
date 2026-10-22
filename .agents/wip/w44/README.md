# Wave 44 parked patches (P25-20..24 release prep). Base: bee44d9.

## w44-r1.patch: P25-20 release checklist + triage, P25-21 release gates from a fresh clone (done)
- 10 files, +1458. Builder checks: ruff, format, pyright 0, lint-imports; pytest tests/unit/release + tooling/checks/tests
  419 passed; tests/contract + tests/unit/parity 1991 passed / 55 skipped; tooling.checks --require-ref ok (13 incl. new
  `checklist`). shellcheck not installed (RS9 adds it to CI).
- New: docs/internals/release-checklist.md (triage at bee44d9: RM1–27, RS1–22, RL1–25; owner-open values are placeholders;
  After v1.0 matches plan 18 §4), scripts/release/release_gates.sh + gates.txt (27 gates), tests/unit/release/
  test_release_gates.py, tooling/checks/checklist.py (+ tests), notes docs/notes/P25/release-checklist.md, release-gates.md.
- Shared: tooling/checks/__init__.py NAMES (w43 also adds security_static + deps_audit: keep all), docs/plan/16-what-we-change.md
  §2.7 rows D-16.920..931 (932–934 unused); the §3.1 range row is for the lead.
- The `audit` and `security` gates fail until w43 lands (intended). The real gates run on the candidate is the owner's
  (needs Docker, Nix, a display, INDUS_REF, PARITY_REF); log it in HANDOFF.
- RM1 = land w43. Owner must rows: RM2–6 security decisions (SEC-4..10), RM7/8 re-locks, RM9–13 one-time decisions,
  RM14 signing, RM15/16 licences, RM17–20 people-strict lanes, RM26 contributor map.

## w44-r3.patch: P25-24 native Windows terminal backend, best effort (built; Windows-only tests not yet run)
- 10 files, +2140/-3. Builder checks: ruff, format, pyright 0 (linux + windows platform), lint-imports; pytest 2565 passed /
  55 skipped / 7 deselected (the 7 `platforms("windows")` tests start real Git Bash). Ran against the main checkout's .venv,
  which lacks mkdocs/acp/wsproto: test_docs_build, contract/acp, contract/web not collected and `licenses` reported SETUP
  here — the integrator's full gate covers these. mkdocs not run (nav unchanged).
- New: src/indus/backends/local_windows.py (`local` on win32: Git Bash lookup, MSYS path conversion both ways, scripts as
  files, job-object kill, UTF-8 console), tests/unit/backends/test_local_windows.py (63 run on Linux), note
  docs/notes/P25/windows-local.md (D-16.950..958 written out; copy into 16-what-we-change.md; 959 unused).
- Changed: backends/registry.py, platform/known_dirs.py, gen_reference.py (+ regenerated environment-variables.md for
  INDUS_GIT_BASH), acceptance-map ACC-Q6-3, getting-started/windows.md, .github/workflows/ci.yml new `windows-local` job
  (continue-on-error: true until first green — owner/lead removes it after).
- Follow-ups: background processes, `indus verify` host, cron scripts and code-execution launcher still POSIX-only on native
  Windows (each needs a Windows branch or a "use WSL2" refusal).
