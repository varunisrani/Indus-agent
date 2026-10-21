# Task: Obligation ledger (P25-08) Phase: 25 Owner: wave-41 builder Q1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — skill reference 03 §8 (the
ledger's shape and statuses). Obligation titles are written from our own product and e2e specs.
Evidence: none.

What it does (own words): keeps "done" honest for the parity pass. An obligation is one
observable claim in our words, proven by the completion of named steps of one scenario
(`OBL-<AREA>-<nnn>`; `version`, `title`, `family`, `scenario`, `steps`, `required`). The manifest
binds each to exactly one owner — the module and symbol that implement it (a dotted Python module
under `src/`, or a TypeScript path) — and to the product tests that check the same claim (`path::name`
or `path > title`). `rules.json` holds intended-difference rules (`scenario` or `*`, `kind`,
`subject` or `*`, `cite`: a D-id or `16 §<n>`, `why`) and reference gaps (`scenario`, `steps`,
`why`).

Statuses, read from the captures: **failed** (an obligation step did not complete on the
product), **not run** (no product capture, or the flow could not run here), **blocked** (a gap
covers one of its steps), **open** (the reference capture is owed, the flow is not compared yet,
alignment failed, or unclassified divergences touch its steps), **verified** (the rest).

Inputs → outputs: `python scripts/parity/ledger.py --check` → problems and `ledger self-test:
ok|n problem(s)` (exit 1 on problems); `--ledger [--cache] [--ref] [--out]` → `ledger: verified n,
open n, blocked n, failed n, not run n` and one line per obligation; `runner.py --ledger` prints
the same after a run.

Files: `parity/ledger/obligations.json` (18 obligations, 12 scenarios), `parity/ledger/manifest.json`,
`parity/ledger/rules.json`, `scripts/parity/ledger.py`, `tests/unit/parity/test_ledger.py`.

Edge cases / failure modes:
- owners are checked by reading the source (Python AST top-level names; TypeScript `export`
  declarations or export lists), never by importing product modules;
- citations are checked against `16-what-we-change.md` as it is (its `| D-16.n |` rows and its
  numbered headings), so a renumbered or removed D-id fails the rule;
- an obligation whose flow failed after its own steps completed is still judged on its steps.

Our design differences / decisions:
- `rules.json` starts empty: no reference capture exists yet, and rules are written when a
  measured divergence is classified (P25-09), not from memory.
- REA's own ledger (`build_reconstruction_obligation_ledger`) needs an exported Evidence bundle of
  both sides; it is rechecked when the reference captures exist. Our `--ledger` report is the
  working progress bar.

Tests we will write: real ledger self-test green; every scenario proves an obligation (removing
one fails); a rule citing a missing D-id fails (and a non-citation, and a rule without why);
a manifest pointing at a missing symbol, module or TS export fails; unknown steps, unresolved test
ids, duplicate bindings, bad ids, duplicate obligations, gaps without reason or with unknown steps
fail; statuses from synthetic captures (open, failed, blocked, verified, open-unclassified, not
run); `--ledger` prints counts per status; `--check` ok.

## Decisions for the lead
- The current report (here, 2026-10-21): open 16 (reference captures owed), not run 2 (desktop),
  verified 0, blocked 0, failed 0 — with the dashboard captures imported from the REA runs; a
  plain `--all --dry-run` leaves the two dashboard flows `not run` (open 14, not run 4).
- D-16.61 is settled (REA registered), so nothing reads "blocked: REA not registered"; the
  reference captures are the owner's to make in `~/rea-lab` (the runner never runs the reference).

Compare checklist (filled after the rewrite; reopened skill reference 03 §8):
- [x] obligation shape and titles as observable claims (`parity/ledger/obligations.json`)
- [x] exactly one binding per obligation, owner module/symbol, tests (`tests/unit/parity/test_ledger.py::test_a_manifest_pointing_at_a_missing_symbol_fails`, `::test_unknown_steps_tests_and_duplicate_bindings_fail`)
- [x] statuses verified / open / blocked / failed / not run (`::test_statuses_from_captures`)
- [x] self-test: every scenario proves one, steps exist, owners export, tests resolve, rules cite, gaps say why (`::test_real_ledger_self_test_is_green` and the failure cases)
- Not taken: REA's ledger build (needs the reference Evidence bundle).

## Status

Done (2026-10-21).
