# Task: Per-job notepad (P14-17) Phase: 14 Owner: wave-17 builder C1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `cron/notepad.py` (limits,
injection). Closed before writing. Evidence: none.

What it does (own words): a small durable key/value store per job, kept in the `job_notes` table
of `<home>/cron/executions.db`, for cursors and watermarks a job carries from run to run.
Values ≤ 16 KiB (UTF-8 bytes), keys 1–128 characters, and per job ≤ 64 KiB counting keys and
values. A write that would break a limit raises `NotepadLimit` and leaves the store unchanged.
Each run's prompt gets a short block listing the entries (P14-10 calls `render_block`); a job
without entries gets an empty string so its prompt is byte-identical. The agent writes it through
`indus cron notepad <job> get|set|unset|list` (C3, P14-19) via `terminal` — no model tool.
Removing a job clears its notepad (remove hook registered by `CronContext`).

Inputs → outputs: `Notepad(db, clock)`: `get(job, key) -> str | None`, `set(job, key, value)`,
`unset(job, key) -> bool`, `list(job) -> list[NoteEntry]`, `clear(job) -> int`,
`render_block(job) -> str`.

Files: `src/indus/cron/notepad.py`, `tests/unit/cron/test_notepad.py`.

Edge cases / failure modes:
- replacing a key counts only the new value against the job total;
- control characters in values are kept verbatim in storage; the prompt block shows each value
  on its own line, newlines indented, so one value cannot fake another entry;
- an unreadable database → `render_block` returns "" with a warning (a run never fails on it).

Our design differences / decisions:
- the table lives in `executions.db` (plan) instead of a database of its own;
- block wording ours.

Tests we will write: 16 KiB + 1 rejected and store unchanged; exactly 16 KiB accepted; 64 KiB job
total; replace within total; key limits; list order; block content/empty; job removal through the
store clears it.

## Decisions for the lead
- none.

Compare checklist (filled after the rewrite; reopened `cron/notepad.py`):
- [x] limits and untouched store on refusal (`tests/unit/cron/test_notepad.py::test_value_limit`,
  `::test_job_total_limit`, `::test_bad_keys`)
- [x] empty notepad → empty block; cleared with the job (`::test_render_block`,
  `::test_removing_job_clears_notepad`)
- Deliberate differences: the verb is `unset` (plan) and keys may not hold control characters, so
  the prompt block cannot be forged by a key; values are indented under their key.
- Not taken: nothing.

## Status

Done (2026-10-19). Files: `src/indus/cron/notepad.py`, `src/indus/cron/context.py`,
`tests/unit/cron/test_notepad.py`.
