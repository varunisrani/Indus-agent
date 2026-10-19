# Task: Turn liveness watchdog (P17-04) Phase: 17 Owner: wave-23 builder K1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/turn_facade_lease.py`
(watchdog wiring, commit under the activity lock, deactivate after abort: skim),
`agent/turn_liveness.py` (settings, sampling, commit, surfaces). Research 01 §3.23.
Evidence: none.

What it does (own words): a hosted turn that shows no activity for `agent.turn_liveness_s`
seconds (600; `<= 0` turns it off) is stopped, polled every 15 s. Activity is whatever proves the
turn is moving: model deltas, tool heartbeats and progress, any turn event the host relays; while a
person is being asked something (approval, clarify) the turn counts as active, however long the
wait. Lease renewal is not activity (a wedged turn would renew forever). The abort decision is made
on an observation — the turn's generation and the time of its last activity — and committed only
after re-reading both under the activity lock: if anything moved in between, or the turn ended or
was replaced, nothing happens. A committed abort interrupts the turn through the host's single
exit funnel with reason `stalled` (the turn ends `turn.done{reason: interrupted_by_system,
failure: stalled}`), then stops the lease refresh so a wedge that ignores the interrupt is freed by
the lease's expiry.

Inputs → outputs: an `ActivityMeter` fed by the host (touches, events, a "waiting on a person"
probe), `LivenessSettings` (the P08-16 reader of `agent.turn_liveness_s`), a clock, an abort callback
and an optional deactivate callback → at most one committed abort per turn.

Files: `src/indus/service/turn_liveness.py`, `tests/unit/service/test_turn_liveness.py`.

Interface: `ActivityMeter(clock)` with `begin(generation)`, `end(generation)`, `touch(note="")`,
`observe_event(event)`, `observe() -> Activity | None`, `lock`; `LivenessWatchdog(meter, settings,
*, clock, abort, waiting=None, deactivate=None)` with `tick() -> bool`, `start()`, `stop()`;
`STALLED`, `stall_failure(idle_s)`.

Edge cases / failure modes:
- activity at 599 s resets the idle clock; the next abort can only come 600 s after it.
- activity that lands between observe and commit vetoes the abort (no cancellation).
- the turn ended or a newer generation began between observe and commit → no abort.
- the abort callback refuses (returns False, or raises) → no abort recorded, watchdog keeps going.
- a person-wait of any length: never aborted; idle counts from the end of the wait.
- settings off → no watchdog at all.

Our design differences / decisions:
- this service watchdog watches the hosted turn as a whole (claim to settle, the admission worker's
  ticket); the P08-16 core watchdog keeps watching the agent loop inside it. Both read the same
  setting; the service one adds the person-wait rule and the lease deactivation.
- `stalled` travels as the failure reason of an `interrupted_by_system` exit: `ExitReason` is a
  closed protocol set and new protocol names are P17-25's to add.
- no thread-stack dump on a stall (logged reason and idle time only).

Tests we will write:
- fake clock: idle 600 s aborts; activity at 599 s resets; race where activity lands between
  observe and act → no abort; long approval wait not aborted; generation change vetoes; settings
  off → none; abort callback refusing → keeps watching; deactivate runs once after a commit.

## Decisions for the lead
- Add `stalled` to `ExitReason` with P17-25 if surfaces should show it as its own exit; until then
  the failure reason carries it.

Compare checklist (filled after the rewrite; reopened `agent/turn_liveness.py`):
- [x] defaults 600 / 15, `<= 0` off, invalid values fall back (reused `core.liveness.LivenessSettings`; test: `test_turn_liveness.py::test_settings_off_means_no_watchdog`)
- [x] observation bound to (generation, stamp), revalidated under the lock (test: `::test_activity_between_observe_and_act_vetoes_the_abort`)
- [x] after a commit the lease renewal stops, once (test: `::test_idle_600_s_aborts_once`)
- [x] a declined abort keeps polling (test: `::test_a_refused_abort_keeps_watching`)
- Deliberate differences: person waits count as activity explicitly; no stack dump; no sleep
  discount (the reference subtracts time the host was asleep; our clock's monotonic time does not
  advance in suspend on Linux, recorded as a follow-up for macOS).
- Not taken: metrics counter for watchdog aborts (P18 observability).

## Status

Done (2026-10-19). No shared files.
