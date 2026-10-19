# Task: Curator phase 2, consolidation and backups (P09-19) Phase: 09 Owner: builder slice F

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/curator.py` (fork setup, the
candidate list, classification of removed skills, rename summary, cron reference rewrite — prompt
constants read for meaning only, never copied), `agent/curator_backup.py` (scope of the snapshot,
excluded names, keep count, pre-rollback snapshot, staged extract). Evidence: none.

What it does (own words): an optional second pass (off by default; `--consolidate` forces one)
where a model, with only `skills_list`, `skill_view` and `skill_manage`, reads the skills the curator
manages and folds narrow overlapping ones into class-level umbrella skills. Absorbed skills are
archived through `skill_manage delete` with `absorbed_into`, never deleted. After the pass we diff
the managed set, classify each skill that disappeared (absorbed into X, or pruned), print an
explicit `old → new` map, and ask every registered reference source (cron, P14) to follow the
renames. Before the pass the live skill tree is snapshotted to
`<home>/skills/.curator_backups/<utc id>/skills.tar.gz` with a `manifest.json`; the newest
`curator.backup.keep` (2) snapshots are kept. A whole-tree rollback takes a "before rollback to
<id>" snapshot first, so it can itself be undone.

Inputs → outputs: managed skills (P09-18 jurisdiction), the `curator` auxiliary task (P08-17;
timeout 600 s unless configured; iteration cap 9,999), the tool registry restricted to the three
skill tools → archived skills, a rename map, reference rewrites, `ConsolidationResult` for the run
report; backups: `snapshot(scope, reason)`, `list_backups`, `prune_backups`, `rollback(scope, id)`.

Files: `src/indus/learning/curator/consolidate.py`, `src/indus/learning/curator/backup.py`,
`tests/unit/learning/curator/test_consolidate.py`, `tests/unit/learning/curator/test_backup.py`;
shared: `src/indus/providers/auxiliary.py` (`AuxClient.call` takes `tools` / `tool_choice`).

Edge cases / failure modes:
- no candidates → no model call, summary "skipped (no candidates)".
- the model calls any other tool (terminal, write_file) → dispatch answers "Unknown tool" from the
  restricted registry; nothing runs.
- `skill_manage` not installed (slice D not merged) → the pass still runs read-only and the report
  says writes were unavailable.
- model error / timeout → recorded in the report, never raised; phase 1 results stand.
- a removed skill with `absorbed_into` naming a skill that exists → consolidated; `""` → pruned;
  no declaration → a tool-call audit (a write on a surviving skill that mentions the removed name),
  else pruned.
- dry run → a "report only" preface; writes still go through the same tools, so the preface asks
  the model not to write and the report lists any write it made anyway.
- backups: disabled → snapshot returns None and rollback is refused with a clear message; tar
  members with absolute paths or `..` refused; failed extract → staged entries moved back; the
  snapshot being restored is protected from the prune of the safety snapshot; same-second ids get
  `-NN` suffixes.
- excluded from snapshots: `.curator_backups`, `.archive`, `.hub`, `.locks`, `.git`, the ledger
  file and blobs, regenerable cache folders.

Our design differences / decisions:
- the fork is a small tool loop over the auxiliary client with a dispatch-side whitelist (the
  registry holds only the three tools), not a full `Agent`; write origin is bound to
  `background_review` so slice D's review guards apply; read-before-write evidence is per run.
- classification uses the delete declaration and the tool-call audit only (no YAML block in the
  final answer to parse); the prompt asks for a plain rename list in the final answer instead.
- rename rewriting goes through `SkillReferenceSource.rewrite(renames, dropped)` on every source.
- the auxiliary client gained a `tools` keyword (shared, three lines) — side tasks had no way to
  offer tools.

Tests we will write: fake model merges two skills → the absorbed one is in the archive, rename map
printed, fake reference source rewritten; a scripted `terminal` call is refused; no candidates → no
call; model error recorded; backup taken before the pass and the third backup prunes the oldest;
rollback restores bytes and leaves a pre-rollback snapshot; backups disabled → rollback refused;
unsafe tar member refused.

## Wiring for the lead
- `lifecycle.run_curator(scope, consolidate=…, step_factory=…)` calls
  `consolidate.run_consolidation(scope, step)`; `step_factory(scope)` returns a `ModelStep`. The
  CLI passes `cli.commands.curator.default_step_factory` (an `AuxStep` over a fresh `AuxClient` on
  the main route); the service should pass one over its own `AuxClient` (`AuxStep(service.aux)`)
  when it registers `curator_check`. Without a step the pass is skipped and the report says so.
- Slice D's `skill_manage` is picked up from `indus.learning.skills.manage_tool.register_tools`.

## Decisions for the lead
- `AuxClient.call(..., tools=…, tool_choice=…)` added (shared file `providers/auxiliary.py`).
- The pass is not a full forked `Agent` (no prompt-cache parity needed: the curator prompt is its
  own conversation).

Compare checklist (filled after the rewrite; reopened `agent/curator.py`, `agent/curator_backup.py`):
- [x] only the three skill tools reachable (test: `test_consolidate.py::test_other_tools_refused`)
- [x] absorbed skills archived, rename map printed (test: `test_merge_archives_and_maps`)
- [x] reference sources rewritten (test: `test_merge_archives_and_maps`)
- [x] snapshot before the pass; keep 2 (test: `test_backup.py::test_keep_prunes_oldest`,
  `test_consolidate.py::test_run_curator_snapshots_before_pass`)
- [x] gap found: a skill's own `.git`, caches and virtualenvs are left out of snapshots, so a
  rollback lost them — fixed by carrying them over from the folder before, pinned by
  `test_backup.py::test_rollback_keeps_left_out_folders`
- [x] rollback takes a pre-rollback snapshot and restores bytes (test: `test_rollback_restores`)
- [x] disabled backups refuse rollback (test: `test_disabled_refuses`)
- [x] unsafe members refused (test: `test_unsafe_member_refused`)
- Deliberate differences: no structured YAML block; the cron jobs file is not copied into the
  snapshot (P14 owns cron and rewrites through the reference source; snapshotting it belongs there).
- Not taken: renaming a staging folder that could not be emptied to a separate "unrestored" name
  (we report its path and leave it; `prune_backups` only clears staging folders on the next run, so
  the error message tells the person to move the copies first).

## Status

Done (2026-10-19). Shared: `providers/auxiliary.py` (`tools`, `tool_choice` keywords).

## Integration (lead, P09 wave 2 landing)
The pass now drives the real `skill_manage` (the test registry is `consolidate.curator_registry`), with the ledger actor bound to `curator`; `test_merge_archives_and_maps` also checks the two ledger entries. D-16.157.
