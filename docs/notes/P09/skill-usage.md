# Task: Usage telemetry and write provenance (P09-12) Phase: 09 Owner: P09 slice B builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/skill_usage.py` (record
fields, states, the locked update, bundled/hub exclusion, curator-managed test),
`tools/skill_provenance.py` (all). Closed before writing.
Evidence: none.

What it does (own words): a sidecar `<home>/skills/.usage.json` keeps one record per skill:
counters for loads, views and patches with their last times, when it was created, its lifecycle
state (`active`, `stale`, `archived`), whether the user pinned it, when it was archived, its origin
and its last activity. Every update takes a cross-process lock and replaces the file atomically, so
two processes bumping at once lose nothing. Bundled skills (named in `.bundled_manifest`) and hub
skills (named in `.hub/lock.json`) are never written: they are not ours to age. Provenance: a
context variable carries who is writing (`foreground`, `background_review`, `learn`). Only a create
made by the background review records `origin: agent`, which is what puts a skill under the
curator's care (R07.6); foreground and `/learn` creates record `learn`. A skill without a record or
with `origin: null` is hand-written or older than tracking. `adopt` (P09-20) sets `agent`
explicitly. Telemetry never fails the caller: a corrupt sidecar is set aside, a warning logged, and
an empty one written on the next update.

Inputs → outputs: skill name + event, clock, context origin → updated record; `get(name)` → record
with every field present; `load()` → all records.

Files: `src/indus/learning/skills/usage.py`, `tests/unit/learning/skills/test_usage.py`.

Edge cases / failure modes:
- missing file → empty; corrupt JSON or a non-object → copied to `.usage.json.corrupt`, warning,
  empty records;
- record with missing or wrong-typed fields → back-filled with defaults on read;
- lock timeout or write error → warning, the event is dropped, the tool call goes on;
- bundled / hub skill → no write (reads still return a default record);
- negative or non-integer counters on disk → treated as 0.

Our design differences / decisions:
- `origin` values are `agent` / `learn` / `null` (the phase names them); the write-origin context
  variable lives here and is the one the memory tool and review fork share (slice A should import
  `indus.learning.skills.usage.WRITE_ORIGIN` / `write_origin()`, or the lead moves it to a neutral
  module at integration);
- a second context variable `REVIEW_ATTENDED` (default False) tells guards whether a review run is
  attended (`/refine`) — the P09-02 staging rule needs it;
- the lock is a sibling `.usage.lock` file through `foundation.fsio.FileLock` (re-entrant per
  thread, cross-process).

Tests we will write:
- counters and timestamps per event (fake clock); origin per context value; pinned / state setters;
- bundled and hub skills not written; corrupt sidecar → warning, empty, never raises;
- two processes (spawn) bumping concurrently lose no increment.

## Decisions for the lead
- The hub lock file is assumed at `<home>/skills/.hub/lock.json` with an `installed` mapping (or a
  top-level mapping) of names; P13 owns the real shape and may change `hub_names()`.

Compare checklist (filled after the rewrite; reopened both files):
- [x] record fields and states (test: `tests/unit/learning/skills/test_usage.py::test_counters_and_times`)
- [x] only background-review creates are agent-origin (test: `::test_origin_per_context`)
- [x] bundled/hub excluded (test: `::test_bundled_and_hub_not_written`)
- [x] concurrent processes lose no increment (test: `::test_two_processes_lose_no_increment`)
- Deliberate differences: the reference stores provenance as `created_by`; we use `origin` with the
  phase's values. The reference has a separate "suppressed names" list; not needed here.
- Not taken: lifecycle events to plugins (P13 hook bus).

## Status

Done (2026-10-19). Shared: none.

## Integration (lead, P09 wave 1 landing, 2026-10-19)

Integration: the second write-origin context var (`WRITE_ORIGIN`, `REVIEW_ATTENDED` and their helpers)
is gone; provenance reads `learning.pending.current_origin()` and the review fork binds
`pending.bind_origin("background_review", attended=False)` (`/refine`: `attended=True`). D-16.141.
