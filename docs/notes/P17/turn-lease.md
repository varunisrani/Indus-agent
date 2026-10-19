# Task: Durable turn lease (P17-03) Phase: 17 Owner: wave-23 builder K1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/turn_facade_lease.py`
(acquire, refresh tick, wait notices, loss interrupt, early results, carrying an interrupted
follow-up), `gateway/turn_lease.py` (in-process cap 512, idle eviction, identity-checked release),
`hermes_state_compression.py` (the lease-row claim, acquire wait loop, refresh, release only).
Research 01 §3.23; R03.20, R06.2.
Evidence: none.

What it does (own words): one process at a time may load, run and flush a session. The right is
a row in the store's `turn_leases` table naming the holder (pid, process start time, pid
namespace or host id, host, a per-lease nonce), the generation the holder runs and an expiry.
TTL 300 s; the holder renews every 60 s, each renewal waiting at most 20 s for the write lock and
never past "expiry − 2 s" (a renewal still waiting after that could land after a successor took the
row). A failed renewal past that point, or a renewal that finds the row gone or re-owned, means the
lease is lost: the turn is interrupted with reason `lease_lost`. A second process wanting the
session waits (up to 1,800 s), and the person is told "waiting for another run" at once and every
15 s; a stop ends the wait (the input is dropped); a follow-up message ends it too but the waiting
input is carried into the follow-up's turn, never consumed. An expired row, or one whose holder is
provably dead (same host, same pid namespace, pid gone or start time changed), is reclaimed; a
holder from another pid namespace or host is never probed — only its expiry frees the row
(R03.20). In one process, waiters on the same session are woken directly on release through a
table of at most 512 idle-evictable slots. Release deletes the row only when holder and
generation still match, so a displaced turn can never clear its successor (R06.2). This replaces
the P02 per-session file lock for daemon-hosted sessions; `--local` takes the same lease.

Inputs → outputs: store, session id, generation (or none: one is minted from the ledger's
per-session counter), clock → `HeldLease` (authority deadline, refresher, loss callback) or
`NotAcquired(reason, waited, carried)`.

Files: `src/indus/state/turn_leases.py` (rows), `src/indus/service/turn_lease.py` (acquire / wait /
refresh / loss / local slots), `src/indus/state/schema.py` (shared: new columns on `turn_leases`),
`src/indus/state/session_lock.py` (shared: docstring marks it superseded), 
`tests/unit/service/test_turn_lease.py`, `tests/integration/service/test_turn_lease_multiprocess.py`.

Interface (for K2 and P17-08/18):
- state: `LeaseHolder.current()`, `LeaseRow`, `try_acquire_in(conn, session_id, holder, *, now,
  ttl, generation=None, is_dead=...) -> Acquired | Held`, `refresh_in(conn, session_id, holder_id,
  generation, *, now, ttl) -> bool`, `release_in(conn, session_id, holder_id, generation) ->
  bool`, `get(store, session_id)`, sync wrappers `try_acquire`, `refresh`, `release`.
- service: `TurnLeases(store, *, clock, settings=LeaseSettings(), probe=None)`;
  `await leases.acquire(session_id, *, generation=None, on_wait=None, interrupt=None,
  pending_input=None) -> HeldLease | NotAcquired`; `held.start(on_lost)`, `held.stop_refresh()`,
  `await held.release()`, `held.generation`, `held.deadline`, `held.lost_reason`, `held.waited`
  (another holder, a busy store or a reclaimed row came first: reload the session before the turn);
  `WaitInterrupt.stop()` / `.follow_up()`; constants `LEASE_LOST`, `WAIT_TEXT`.

Edge cases / failure modes:
- the store busy during an acquire attempt is not a holder: retry, no "waiting" notice.
- refresh hits a lock error: a missed tick only while the next tick can still land before the
  authority deadline; otherwise lost now.
- refresh after `stop_refresh()` / release: no loss reported.
- re-acquire by the same holder (same nonce) renews instead of blocking itself.
- a pid reused by another process: start time differs → dead → reclaimed.
- unknown pid namespace on either side → never "provably dead" → expiry only.
- the local slot table above 512 entries evicts idle slots only; live slots may exceed the cap.

Our design differences / decisions:
- the holder id keeps the P02 stamp shape (`pid=..:pidns=..:<host>:start=..:lease=..`) so deletion
  and maintenance guards read it unchanged; namespace "-" when unknown, with the host id in
  `holder_ns` as the spec asks.
- the lease row carries the generation (shared counter with the runtime ledger) and release /
  refresh are keyed by holder **and** generation.
- in-process waiters poll the store every second as well as waiting on their slot, so a holder in
  another process is noticed without signals.
- our own wording for the waiting / timed-out / lost messages.

Tests we will write:
- unit (fake clock): acquire and release; second holder waits, told at once and every 15 s, then
  proceeds after release; timeout after 1,800 s; stop vs follow-up interrupt (carried input);
  expired row reclaimed; dead holder (same namespace, pid gone) reclaimed before expiry; holder in
  another namespace with a live-looking pid not treated as alive, reclaimed only after expiry;
  refresh moves the deadline; refresh failure past expiry → `lease_lost`; lock errors near the
  deadline → lost; re-owned row → lost; release keyed by generation; local slot cap.
- integration: two real processes contend (second waits, proceeds after release); a SIGKILLed
  holder is reclaimed after expiry.

## Decisions for the lead
- Wiring into `SessionService` (take the lease at turn start, release in the turn's `finally`,
  `lease_lost` → `TurnControl.interrupt(generation, hard=True, by_system=True)`) belongs to P17-08
  (daemon host) and P17-18 (`--local`); until then the P02 file lock stays in force.
- Lease timings are constants (no config rows), as the reference.

Compare checklist (filled after the rewrite; reopened the three files above):
- [x] TTL 300 / refresh 60 / patience 20 / margin 2 / wait 1,800 / notice 15 (test: `test_turn_lease.py::test_settings_match_the_spec`)
- [x] renewal fenced by holder (and generation); a miss after stop is not a loss (test: `::test_refresh_after_release_is_not_a_loss`)
- [x] lock errors: retry only while the next tick lands before the deadline (test: `::test_lock_errors_near_the_deadline_lose_the_lease`)
- [x] authority deadline is the committed expiry, moved only by a successful renewal (test: `::test_refresh_moves_the_deadline`)
- [x] busy store during acquire is not a holder (test: `::test_a_busy_store_is_not_a_holder`)
- [x] interrupt while waiting: stop drops, follow-up carries (test: `::test_follow_up_interrupt_carries_the_waiting_input`)
- [x] reclaim on expiry or provably dead same-namespace holder; foreign namespace waits for expiry (tests: `::test_*namespace*`, `::test_dead_holder_is_reclaimed_before_expiry`)
- [x] in-process cap with idle eviction only (test: `::test_local_slots_are_capped_and_evict_idle_only`)
- [x] identity-checked release (test: `::test_release_is_keyed_by_holder_and_generation`)
- Deliberate differences: generation on the row; holder namespace falls back to the host id.
- [x] reload signal after a wait or a busy store (test: `::test_second_holder_waits_is_told_and_proceeds_after_release`, `::test_a_busy_store_is_not_a_holder`) — gap found at compare, fixed by `HeldLease.waited`.
- Not taken: the transcript reload itself (the host does it on `waited`, P17-08), the mid-turn
  rebind on compression rotation (our rotation keeps the logical id as an alias).

## Status

Done (2026-10-19). Shared edits: `state/schema.py` (columns), `state/session_lock.py` (docstring).
Wiring into the session service is handed to P17-08 / P17-18.
