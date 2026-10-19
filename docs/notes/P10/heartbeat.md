# Task: Session heartbeats /heartbeat (P10-26) Phase: 10 Owner: P10 slice K builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/heartbeat.py` (grammar,
firing rule). Closed before writing.
Evidence: none.

What it does (own words): `/heartbeat every <dur> <prompt>` (alias `/hb`) sends a recurring
instruction into the session while it is idle; `/heartbeat list|pause|resume|stop`. Grammar
`[every] <n> <s|m|h|d units>`; the floor is 60 s (shorter is refused). One scheduler per process
polls every 5 s for due heartbeats and due loops across the sessions it hosts (`session_meta.due`).
A heartbeat fires only into an idle session with an empty input queue; the message is an ordinary
user message that frames the instruction as recurring and asks for a brief answer when nothing
changed. Missed ticks coalesce: the next due time is counted from the last fire. Session-scoped
and in-process only (durable schedules are cron, P14); state is restored on resume. A
`heartbeat.fired` event lets surfaces show the turn as a "wake" row.

Inputs → outputs: slash text → reply lines; poll (now) → due heartbeats → injected turn +
`heartbeat.fired`.

Files: `src/indus/core/heartbeat.py`, `src/indus/core/session_scheduler.py` (the shared idle
scheduler and the session host port), `tests/unit/core/test_heartbeat.py`. Shared: events, the
registry entry `/heartbeat`.

Edge cases / failure modes:
- Busy session or input waiting: skipped; fires on the first poll after it is idle.
- Injection refused (a turn started between check and fire): the fire is not counted.
- Pause then resume: the next due time is counted from the resume, so a stale tick never fires
  at once.
- A session that is not attached in this process: its due rows are left alone.

Our design differences / decisions:
- Several heartbeats per session (ids `h1`, …); `stop [id]` stops one or all.
- The scheduler is one asyncio task on the service loop (fake clock in tests), shared with loops.

Tests we will write:
- grammar table (`every 90s` ok, `30s` → floor error, units);
- fires at the due tick on an idle session (fake clock); busy skips then fires when idle;
- pause / resume; restart restores and the next due time comes from the last fire.

## Decisions for the lead
- None beyond the shared scheduler shape.
- Intended differences: several heartbeats per session with ids; `stop` (with `clear` as a
  synonym).

Compare checklist (filled after the rewrite; reopened `hermes_cli/heartbeat.py`):
- [x] Grammar and 60 s floor (`tests/unit/core/test_heartbeat.py::test_grammar`)
- [x] First beat one interval after creation, idle-only, missed ticks collapse (`test_fires_at_the_due_tick_on_an_idle_session`, `test_busy_skips_then_fires_when_idle`)
- [x] A refused injection is rolled back (`test_a_refused_injection_does_not_count`), like the reference's abandon step
- [x] Resume counts from now (`test_pause_and_resume`); restart recomputes from the last fire (`test_restart_restores_and_counts_from_the_last_fire`)
- [x] 5 s poll on the clock; one fire per session per poll (`tests/unit/core/test_loops.py::test_scheduler_task_polls_on_the_clock`, `test_one_fire_per_session_per_poll`)
- [x] Gap found: similarity check flagged the unit table shape — rewritten as one regex with a named group per unit.
- Deliberate differences: several heartbeats per session (ids), `stop` instead of `clear` (`clear` kept as a synonym), state as JSON rows with `due_at` instead of a prefixed key scan.
- Not taken: a sweep over every stored session (only sessions attached in this process are fired; durable schedules are cron's).

## Status

Done (2026-10-19). `heartbeat.fired` event added. `HeartbeatDriver.restore(session_id)` exists for hosts that attach a session after a restart; the stored `due_at` already carries the schedule, so the CLI does not call it yet.
