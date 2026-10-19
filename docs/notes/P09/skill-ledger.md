# Task: Skill mutation ledger (P09-13) Phase: 09 Owner: P09 wave 2 slice D builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/skill_ledger.py` (entry shape,
blob store, rollback, size cap). Closed before writing.
Evidence: none.

What it does (own words): every change to a skill, by anyone, appends one JSON line to
`<home>/skills/.ledger.jsonl`. A line names who changed it (`actor`: `curator`, `agent` or `user`),
what kind of change it was, the skill, some evidence (`absorbed_into`, `archived`, `session_id`,
`file_path`, `rollback_of`), and two file lists: the package's files before and after, each as
`{path, sha256}`. The bytes behind every hash are kept once in `<home>/skills/.ledger-blobs/<sha>`.
The ledger is a record, not a gate: when it cannot be written the change still happens and a
warning is logged.

One entry can be rolled back: the files the change touched get their old bytes back and files the
change created are removed. Before touching anything the rollback checks every blob it needs and
appends its own entry holding the current bytes of every path it will touch (the safety capture);
if that capture or append fails, the rollback refuses and changes nothing. The rollback entry is
itself rollback-able (undo of an undo).

Size: `skills.ledger_max_bytes` (default 5 MiB; `0` = never maintain). After an append that leaves
the file larger than the cap, the ledger is rewritten without unchanged-file pairs (a path with the
same hash before and after), then, if still too big, without its oldest lines until it is at most
three quarters of the cap; the newest entry always stays. Then blobs no line references are deleted
when older than one hour (a blob that new may belong to a capture whose line is not written yet).

Inputs → outputs: `SkillLedger(home, clock, max_bytes)` →
`capture(skill_dir) -> [FileRef]` (raises on IO), `record(action, skill, before=, after_dir=, actor=,
evidence=) -> id | None` (never raises), `entries(skill=, limit=)` newest first, `get(id)`,
`rollback(id) -> RollbackResult`, `maintain()`, `compact()`, `sweep_blobs()`. Actor binding:
`bind_actor("user" | "curator" | "agent")` context manager; default `agent`.

Files: `src/indus/learning/skills/ledger.py`, `tests/unit/learning/skills/test_ledger.py`.

Edge cases / failure modes:
- missing ledger → no entries; malformed lines skipped when listing, kept verbatim on compaction,
  and they stop the blob sweep (an unreadable line may reference blobs);
- rollback of an entry whose paths leave every skills root (hand-edited ledger) → refused;
- a missing blob → refused before any change;
- caches (`__pycache__`, `.venv`, `node_modules`, `.git`, tool caches) are never captured;
- the ledger lock is `<home>/skills/.locks/ledger.lock` (cross-process, re-entrant per thread).

Our design differences / decisions:
- names: `.ledger.jsonl` and `.ledger-blobs/` under `<home>/skills/` (the phase row names them);
- entries keep whole-package manifests at append time and compaction drops unchanged pairs (the
  phase row's order); rollback always works on the changed paths only;
- the safety capture and the rollback record are one entry (action `rollback`, evidence
  `rollback_of`), written before the restore; the reference writes two;
- actor default is `agent` for every model write (review fork included); the curator binds
  `curator` explicitly and the CLI binds `user`;
- paths are stored absolute; rollback accepts only paths inside the profile's skill roots
  (local, `create_dir`, `external_dirs`) or the archive under the local root;
- no curator-backup tarball fill-in for hollow delete entries (P09-19 owns backups; a delete
  entry here captures the whole package itself).

Tests we will write:
- patch then rollback restores bytes; rollback of a create removes the file (and the empty dir);
- safety capture failure (injected IO error) → refused, tree unchanged, no entry added;
- size cap compaction keeps the newest entry; unchanged pairs dropped first;
- blob sweep keeps a young blob and removes an old unreferenced one (injected clock);
- a path outside the skill roots → rollback refused; missing blob → refused.

## Decisions for the lead
- Interface for slice F (curator `ledger`/`purge`/`archive`/`restore`): `SkillLedger.for_scope(scope)`,
  `bind_actor("curator")`, `capture(dir)` before the move, `record("archive", name, before=...,
  after_dir=new_dir)`; `entries(skill=)`, `rollback(id)`.

Compare checklist (filled after the rewrite; reopened `tools/skill_ledger.py`):
- [x] entry fields, blobs once per hash (test: `test_ledger.py::test_entry_shape_and_blobs`)
- [x] rollback restores touched files, removes created ones (`::test_patch_then_rollback_restores_bytes`,
  `::test_rollback_of_create_removes_the_file`)
- [x] safety capture first, fail closed (`::test_safety_capture_failure_refuses`)
- [x] 5 MiB default cap, unchanged pairs first, then oldest, newest kept (`::test_size_cap_keeps_newest`,
  `::test_compaction_drops_unchanged_pairs_first`)
- [x] 1 h blob grace (`::test_blob_sweep_respects_one_hour`)
- [x] paths outside the roots refused (`::test_rollback_refuses_outside_paths`)
- Deliberate differences: one rollback entry instead of pre-rollback + rollback; actor default
  `agent`; no `skills.ledger` on/off switch (the row has none; `ledger_max_bytes: 0` only stops
  maintenance); trim low-water 3/4 of the cap (reference uses its own fraction).
- Not taken: curator-backup package fill for old hollow entries (P09-19 owns backups).

## Status

Done (2026-10-19). Shared: none.

## Integration (lead, P09 wave 2 landing)
The curator reaches this ledger through `curator.ledger_port`, which now defaults to `SkillLedgerPort(scope)` over `SkillLedger.for_scope` (a plugged-in port only overrides it); the consolidation pass binds the actor `curator`. D-16.150.
