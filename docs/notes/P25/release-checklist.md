# Task: Release checklist and bug-bash triage (P25-20) Phase: 25 Owner: wave-44 builder R1

Status: Done (2026-10-22) for the checklist, the triage snapshot and the `checklist` guard; the
rows themselves are open work for the lead and the owner. Landed with wave 44 (integration on main
`7d9a864`, D-16.962): 27 must, 26 should, 26 later, 3 done (RM1, RM3 at `4945b48`; RM30 = F-1).

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — no reference product file
applies. Process only: the clean-room-rebuild skill's reference 08 §1–§4 (agents never tag; triage
buckets, permanent ids, Source and Why columns, counts in headings; "Done in this wave"; one-time
decisions with safe placeholders) and its release-checklist template.
Evidence: none.

What it does (own words): one file, `docs/internals/release-checklist.md`, that the owner works
top to bottom to take the candidate to `v1.0.0`. Section 0 lists the one-time decisions (each with
what hangs on it, the options, our recommendation and the placeholder in force until the owner
answers — never an invented value). Then the fresh-clone gates (P25-21), the canary rehearsal
(P25-22), the release itself (owner only), the post-release smoke and rollback (P25-23), and
"Afterwards". The bug-bash triage collects every open item from the task notes, `HANDOFF.md`, the
lead notes, the acceptance map's Known gaps and Review rows, the parked security findings (SEC-1..11,
SEC2-1..4) and the parity ledger, checks the important ones against the code at `bee44d9`, and files
each under must (`RM<n>`), should (`RS<n>`) or later (`RL<n>`). The `R` prefix keeps them apart from
the inventory's S-ids. "After v1.0" repeats the plan's list (`18-phase-wise-build-plan.md` §4).

A lint keeps the list honest: the new guard `checklist` (part of `python -m tooling.checks`) fails
when an id is duplicated, when an id up to the recorded high-water mark is missing from every table
(an id is never dropped, so it can never be reused), when an id is above the mark, when a heading's
count differs from its rows, when a row has an empty Source or Why, when an `RM` row sits in the
should or later table without the owner's dated approval, or when an After v1.0 D-id from plan 18 §4
is missing.

Inputs → outputs: the sources above → the checklist; the checklist → `checklist` guard result
(ok / FAIL with `file:line: problem`).

Files: `docs/internals/release-checklist.md`, `tooling/checks/checklist.py`,
`tooling/checks/__init__.py` (registers `checklist`), `tooling/checks/tests/test_checklist.py`,
`docs/plan/16-what-we-change.md` (D-16.920 – D-16.934).

Edge cases / failure modes:
- an item fixed since its note was written: listed once under "Checked and already fixed (not
  counted)" with the evidence, so nobody reopens it;
- an item recorded after the last product commit (`226a5ae`, wave 42): open by construction (only
  briefs and handoff commits since);
- old follow-ups grouped by area into one later row; a row says when it was not re-checked;
- a checklist file missing under `--root` (the guards' fixture trees): a note, not a failure;
- an `RM` id moved to should without the owner's quoted, dated approval: a failure;
- the Done table: every row names what closed it and a commit cell (the lead fills the sha).

Our design differences / decisions:
- Ids: one high-water line per series (`Ids assigned: RM.. RS.. RL..`) and every id from 1 to the
  mark appears exactly once in the tables (Must, Should, Later, Done in this wave, Withdrawn). This
  is how "never reused" is checked without history.
- No npm or PyPI publishing (P23-01 no-PyPI guard): the release order is `release.py` (owner) →
  `release.yml` (admit, gate, lanes, docker-publish, publish). The template's npm steps become the
  GHCR/GitHub Release steps.
- Owner-open values stay placeholders in this file only; nothing new is written into packaging.

Tests we will write:
- the real checklist passes; duplicate id; missing id below the mark; id above the mark; count
  mismatch; empty Source / Why; RM in should without approval, and with approval; a Done row
  without a commit cell; an After v1.0 D-id from plan 18 missing; no checklist under the root is a
  note; the CLI entry point runs alone.

## Decisions for the lead
- D-16.920 – D-16.925, D-16.932 (see `16-what-we-change.md`).
- The triage is a snapshot of `bee44d9`; work the rows in waves and move them to "Done in this
  wave" only after the fix is checked in the code (the lead fills the commit).

Compare checklist (filled after the rewrite; reopened skill reference 08 §2–§4):
- [x] buckets must / should / later with permanent ids, Source and Why, counts in headings
  (`tooling/checks/tests/test_checklist.py::test_the_real_checklist_passes`)
- [x] must → should only with the owner's dated approval
  (`tooling/checks/tests/test_checklist.py::test_a_must_row_moved_down_needs_the_owners_dated_approval`)
- [x] "Done in this wave" with what closed it and the commit; "Partly done" line; "Checked and
  already fixed (not counted)"
- [x] one-time decisions with records and placeholders that cannot reach users
- [x] agents never tag, publish or set secrets; the closing line "Ready for `v1.0.0` at `<sha>`"
  waits for a green run
- Deliberate differences: no npm package; the "update from the last nightly" step is our canary
  channel (P25-22).
- Not taken: an issue-tracker export (owner's call after 1.0).

## Triage counts (snapshot `bee44d9`)

Must 27 (security findings and wave 43 landing RM1–RM8; one-time owner decisions RM9–RM16;
release gates that fail today RM17–RM21; CI lanes never run RM22–RM24; perf baseline RM25;
contributor map RM26; open Compare steps RM27), should 22, later 25 (ten Known gaps rows, then
older follow-ups grouped by area), done 0, withdrawn 0; 16 items found already fixed (plus one non-product item), not counted.

## Status

Done (2026-10-22). Shared edits: `tooling/checks/__init__.py` (adds `checklist` to `NAMES`; wave
43 adds `security_static` and `deps_audit` to the same tuple — a union at landing),
`docs/plan/16-what-we-change.md` (D-16.920 – D-16.934 after the D-16.880 – D-16.889 row; the §3.1
range row is the lead's at landing). Handed on: every RM row to the lead and the owner; RM1 is the
wave 43 integrator's first step.

Landed with wave 44 (integrator, on main `7d9a864` after wave 43): `NAMES` keeps all three new
guards; §3.1 range row D-16.920 – D-16.969. Triage refreshed (D-16.962): RM1 and RM3 (SEC-5) moved
to Done at `4945b48` after checking the code; new must rows RM28 (SEC-14) and RM29 (SEC-15), the
open wave-43 findings; the rehearsal findings F-1 = RM30 (fixed at this integration, D-16.960),
F-2..F-5 = RS23–RS26; P25-24's follow-ups = RL26; Source cells that cited the pruned
`.agents/wip/w43/` now cite the threat model and LEAD-NOTES. Marks: RM1–RM30 · RS1–RS26 · RL1–RL26;
`checklist` guard ok.
