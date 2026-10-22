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
