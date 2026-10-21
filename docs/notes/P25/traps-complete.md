# Task: Traps ledger complete (P25-11) Phase: 25 Owner: wave-41 builder G1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — a guard over our own plan and
tests; process source is the skill's planning reference §10 (traps ledger rules).
Evidence: none.

What it does (own words): `python -m tooling.checks traps` reads every trap id defined in a
"traps" section of the plan (the 261 `R<nn>.<n>` ids of `docs/plan/research/*.md` §6, plus any
phase `T<nn>.<n>`) and fails when an id has no ledger row, when a cited test does not exist or is
skipped for good, or when a `Review:` line names no owner task (`P<nn>` or `P<nn>-<n>` inside the
trailing parentheses) or no reason. The skeleton placeholder `not yet planned` warns in the
everyday check and fails with `--strict`. `--strict` (the release gate) passes only reviews the
lead accepted — `Review: accepted by the lead <YYYY-MM-DD>: <why> (<owner>)`, for traps that
cannot be tested — and resolves every cited test against collection (shared with P25-10:
`tooling/checks/collect.py`). The check lists unpinned rows (no test reference) as a note and in
`data["unpinned"]` (`--json`).

Inputs → outputs: plan docs, `docs/internals/traps-ledger.md`, tests → violations, warnings,
the unpinned list.

Files: `tooling/checks/traps.py`, `tooling/checks/tests/test_traps_complete.py`,
`docs/internals/traps-ledger.md` (42 placeholder rows filled), `tooling/checks/__main__.py`
(`--strict` help).

Edge cases / failure modes: an owner suffix that is not a task id (`(someday)`) fails; an
accepted review with no reason fails; `[param]` references must name a real parameter id in
strict mode (the fixture's `[layers]` reference is plain under strict).

Tests: removing one row fails with its R-id; an unresolved test path fails; Review without an
owner task fails; placeholder warns / fails strict; only lead-accepted reviews pass strict; an
accepted review needs a reason; strict fails a pinning test skipped here; unpinned rows listed;
the real ledger passes with 261 research ids.

## Ledger rows (this task)

Filled from the phases' "Traps covered" tables, pinning existing tests only (no new tests):
R01.2, R01.13, R01.14, R01.15, R01.17, R01.18, R02.16, R02.28, R03.2, R03.26, R05.2, R05.6, R05.7,
R05.8, R05.10, R05.12, R05.13, R05.16, R05.17, R05.19, R05.20, R05.21, R07.21, R08.3, R08.15,
R08.16, R09.1, R09.2, R09.3, R09.4, R09.5, R09.22, R09.23, R10.13, R10.30.

Unpinned (Review with owner and reason, 9): R03.28 (progress leases not built; unassigned, P02
follow-up), R07.30 (doc drift: candidate for a lead-signed accepted review), R08.20 (P20-02),
R08.22 (P23-15, signing open), R10.17 / R10.19 / R10.20 (CI classifier and per-file runner, P23),
R06.26 and R08.11 (left as placeholders on purpose: the P24 integration patch rewrites exactly
these rows; take P24's lines when it lands).

## Decisions for the lead
- Placeholder reviews warn (not fail) in the everyday check so today's tree stays green while P24
  owns R06.26 / R08.11; `--strict` fails them. Flip to always-fail after P24 lands if wanted.
- Accepted-review syntax `Review: accepted by the lead <date>: <why> (<owner>)`; only the lead
  writes it. Proposed candidate: R07.30.

## Results

`python -m tooling.checks traps`: ok, 2 warnings (R06.26, R08.11). `traps --strict`: 23 non-accepted
Review rows + the 2 placeholders + 4 references skipped in this container (non-root POSIX, pwsh).
`test_traps_complete.py`: 10 passed.

## Status

Done (2026-10-21).
