# Task: Pending writes and the write-approval gate (P09-03) Phase: 09 Owner: P09 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/write_approval.py` (gate
decision, staging, replay). Closed before writing.
Evidence: none.

What it does (own words): memory and skill writes can be held for a person to approve. The gates
are `memory.write_approval` and `skills.write_approval` (default off; an unreadable value counts as
off). With a gate on: a memory write from the foreground of an interactive CLI turn is asked inline
(approve / reject; entries are short); every other surface, every unattended run and the review fork
stage the write instead. Skill writes always stage when their gate is on (too large to approve
inline). Separately from the gates, an unattended review fork's memory `patch` / `remove` always
stage (R07.4). A staged write is one JSON file `<home>/pending/<subsystem>/<id>.json` (0600, atomic)
holding `PendingWrite{id, subsystem, payload, summary, origin, created_at, seq, pinned_entry?}`.
A staged patch/remove records the full entry it targeted; approval replays the change only while
that exact entry still exists, otherwise it refuses and the record stays. Approve or reject takes
one id, several, or `all` (creation order). Approval output lists the full text of every entry
overwritten or removed; a skill approval shows a unified diff (cut to 3,000 characters on chat
surfaces, whole in the CLI). Records survive restarts (they are files). The review summary never
reports a staged write as done (it reads `staged: true` from the tool result).

Public interface for slice B (skills) — kept small on purpose:
- `PendingStore(home, clock)` → `.stage(subsystem, payload, *, summary, origin, pinned_entry=None)`,
  `.list(subsystem)`, `.get(subsystem, id)`, `.discard(subsystem, id)`, `.count(subsystem)`;
- `gate_on(scope, subsystem) -> bool` and `decide(subsystem, *, gate, origin, can_ask) -> Decision`
  (`allow` | `ask` | `stage`);
- `approve(store, subsystem, ids_or_all, apply) -> list[Approval]` where `apply(PendingWrite) ->
  Applied(ok, message, overwritten=(...), diff="")`; `reject(store, subsystem, ids_or_all)`;
- `render_approval(approval, *, surface)` and `skill_diff(before, after, path, *, surface)`;
- write origin: `bind_origin(origin, *, attended)` (context manager), `current_origin()`,
  `unattended_review()`.

Inputs → outputs: subsystem, payload (JSON-able), summary, origin → files under
`<home>/pending/`; approval results with texts and diffs.

Files: `src/indus/learning/pending.py`, `tests/unit/learning/test_pending.py`.

Edge cases / failure modes:
- an unreadable or malformed record → skipped in `list` with a WARNING, never raises;
- `approve` of an unknown id → a "not found" result, others still run;
- the apply callback raises → refused, record stays, error logged;
- two records with the same timestamp (fake clock) → ordered by `seq` (creation order);
- a staging failure (disk) raises to the caller, which denies the write (fail closed).

Our design differences / decisions:
- `seq` field for a total creation order (the reference sorts on time only);
- ids are 10 hex characters; `all` approves in creation order and stops at nothing (each record
  independent; failures stay pending);
- the skill diff renderer takes before/after texts (slice B computes them), not a payload.

Tests we will write:
- stage → new store object → list shows it; approve applies and removes the record;
- target entry edited after staging → approve refused, record stays;
- reject deletes the record only; `all` applies in creation order;
- skill diff truncation at 3,000 chars on the chat renderer, full on CLI;
- gate decision table (off, on+foreground CLI, on+gateway, on+review, skills);
- origin context var default and binding.

## Decisions for the lead
- `memory.write_approval` row added by this slice; `skills.write_approval` belongs to slice B's
  `skills` section (`gate_on` reads it with default off until then).

Compare checklist (filled after the rewrite; reopened `tools/write_approval.py`):
- [x] gate off → allow; skills always stage; background stages; CLI foreground asks (test: `test_pending.py::test_decision_table`)
- [x] record per file under pending/<subsystem>/ (test: `test_pending.py::test_stage_survives_restart`)
- [x] pinned entry refusal keeps the record (test: `test_pending.py::test_edited_target_refuses_and_keeps_record`)
- Deliberate differences: no gist heuristics for skills here (slice B summarises its own writes);
  staged-hint wording names `indus memory pending` / `/memory pending` everywhere.
- Not taken: batch skill diff folding (slice B renders its batch by calling `skill_diff` per file).

## Status

Done (2026-10-19).

## Integration (lead, P09 wave 1 landing, 2026-10-19)

Compare redone at integration (lead reopened `tools/write_approval.py`, then closed it):
gate off allows, skills always stage, the review fork stages, unknown config values mean off. One
gap fixed: the reference asks inline for any memory write that is not the review fork's, and ours
staged `/learn` turns; `decide` now asks for `foreground` and `learn` alike when someone can answer
(`test_pending.py::test_decision_table`). `skills.write_approval` config row added (schema +
`test_schema.py`). The one write-origin context var is this module's; `skills/usage.py` uses it.
Differences recorded as D-16.140, D-16.141.
