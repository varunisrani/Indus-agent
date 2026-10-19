# Task: Delivery ledger (P15-19) Phase: 15 Owner: wave-2 builder W1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/delivery_ledger.py` (all).
Research `06-gateway.md` §3.13–3.15 (finals, retries, recovery), trap R06.5. Closed before writing.
Also the P15 wave 1 landing notes (open seams: `OutboundSender` → `send_with_retry` + ledger,
R06.5 marker after the ledger write).
Evidence: none.

What it does (own words): a chat turn's final reply is written to a durable table before anything
is sent, so a crash between "reply produced" and "platform said yes" never loses it silently. A
row walks `pending` (written before any send) → `attempting` (written just before the platform
call) → `delivered` (only on success) or `failed` (a definitive refusal) → `abandoned` (out of
attempts, too old, or the whole chat is gone). On the next boot the sweep looks at rows whose
owning process (pid + process start time) is dead: a `pending` row was never sent, so it goes out
plainly; an `attempting` row may already be on screen, so it goes out with a "may be a duplicate"
label; a `failed` row is retried. While the gateway runs, a failed row is retried in-process
after 30 s and then 120 s, but the last allowed attempt is kept for the next boot; a row that
failed only because the adapter was not connected waits for the adapter to come back and then goes
out with a "platform reconnected" label; a rate-limit refusal waits the platform's stated time
(default 60 s, capped at 15 min, plus 2 s) and goes out with a "part may have arrived" label; a
boot inside that wait adopts the row without spending an attempt. A whole-chat death is never
retried. Every ledger call is wrapped: a database error is logged and the send goes on.

The durable active-turn marker on the route (`gateway_routes.active_turn_token`) is cleared only
after the turn's final reply has been through the sender (so its ledger row exists) — R06.5.

Inputs → outputs:
- `indus.state.delivery_ledger` (table `delivery_ledger`, declared in `state/schema.py`):
  `LedgerRow`, `obligation_id(session_key, message_ref, content)`, `record_pending`,
  `mark_attempting`, `mark_delivered`, `mark_failed`, `mark_abandoned`, `claim`, `adopt`,
  `get`, `list_open`, `rows_for_session_key`, `prune`. Pure store code, no policy.
- `indus.gateway.delivery.ledger.DeliveryLedger(store, *, clock, enabled=True, is_alive=…)`:
  `for_scope(scope, store)` (None when `gateway.delivery_ledger` is false); the sender calls
  `open / attempting / delivered / failed`; `bind(lookup)` gives it the per-platform senders;
  `recover()` is the boot sweep (P15-26 calls it); `platform_reconnected(platform)` is the
  reconnect hook (P15-29 calls it); `retry_due()` + internal timers for in-process retries;
  `aclose()` cancels timers. Prefix constants `DUPLICATE_PREFIX`, `RECONNECTED_PREFIX`,
  `FLOOD_PREFIX` (our wording). `FAULT_HOOK` lets a test kill the process at a named point.
- `OutboundSender(…, ledger=None, rng=random.random)`: finals carrying `session_key` /
  `message_ref` are ledgered; every text piece goes through `send_with_retry` (P15-04) with the
  adapter's inline wait cap; on failure the report carries the undelivered pieces and the ledger
  row stores them, so a retry sends only those (R06.4).

Files: `src/indus/state/delivery_ledger.py`, `src/indus/gateway/delivery/__init__.py`,
`src/indus/gateway/delivery/ledger.py`, `tests/unit/state/test_delivery_ledger.py`,
`tests/integration/gateway/test_ledger_recovery.py`, `tests/unit/gateway/delivery/test_ledger.py`.
Shared: `src/indus/state/schema.py` (table + index), `src/indus/gateway/outbound/sender.py`,
`src/indus/gateway/outbound/ports.py`, `src/indus/gateway/outbound/replies.py`,
`src/indus/gateway/core/admission.py` (R06.5 gate; finals carry their ledger key),
`src/indus/foundation/config/schema.py` (`gateway.delivery_ledger`) + its test,
`tests/unit/gateway/outbound/test_sender.py`, `tests/unit/gateway/core/_kit.py`.

Edge cases / failure modes:
- the database is locked, missing or corrupt → the call logs and returns; the send happens.
- the same final recorded twice (same key, ref and content) → one row, reset to `pending`.
- a split reply fails part way → the row keeps the undelivered pieces; retries send only them.
- read/write timeout (may have landed) → `failed` as uncertain; a retry carries the duplicate label.
- the platform is not connected at boot → its rows are left alone (no attempt spent) until it is.
- a row owned by a live process (another gateway, a pid that is still us) is never claimed.
- the pid was reused (start time differs) → the owner counts as dead.
- 24 h old → abandoned; delivered/abandoned older than 7 days → pruned; more than 500 rows →
  oldest delivered first, then abandoned, then the rest.
- the silence rule sent nothing → no row.

Our design differences / decisions:
- Row id = first 32 hex of sha256 over `session_key \0 message_ref \0 content`; `message_ref` is
  `<session id>:<turn id>` of the turn whose final it is (unique per turn).
- The stored content is the masked text (redaction runs before the row is written): secrets never
  land in the ledger.
- Column `pieces_json` (ours): the undelivered, already-formatted pieces of a partly sent reply.
  Columns `error_kind`, `retry_at` (ours) hold the failure class and when a retry is due, instead
  of re-parsing error text.
- Prefixes: duplicate (restart or uncertain send), reconnected, flood. A row refused definitively
  (never seen by the platform) is retried without a label: it cannot be a duplicate.
- With a ledger the sender sends no "delivery failed" notice after exhausted retries: the ledger
  is still going to deliver the reply. Without a ledger the P15-04 notice stays.
- Only turn finals are ledgered (not command answers, notices, acks, cron output or `indus send`:
  P14 keeps its own attempt ledger for cron).
- The marker gate: `_finish` waits (≤ 300 s) for the turn-end listener that delivers the final,
  then clears the marker. A turn that ended without a result (cancelled, raised) has no final to
  wait for.

Tests we will write: state table round trip, transitions, prune (7 days, 501st row drops the
oldest delivered first), abandon at 24 h / 3 attempts; ledger policy: retry schedule 30 s then
120 s then reserved for boot (fake clock), flood wait with cap and adoption without spending an
attempt, reconnect-only rows on `platform_reconnected`, whole-chat death never retried, DB error
still sends; sender: partial split stores and resumes only the remainder; integration (subprocess
SIGKILL through `FAULT_HOOK` / the adapter): before pending → 0, between pending and attempting →
1 plain, during the await → 1 labelled, after delivered → 0; R06.5: the marker is still set while
the final is being delivered and clear after.

## Decisions for the lead
- `gateway.delivery_ledger` (bool, default true) added to `GatewaySection`.
- Only turn finals are ledgered (above). Command replies and notices are not.
- Sender: with a ledger, no "delivery failed" notice after exhausted retries.
- `FinalReply` gains `session_key`, `message_ref`, `allow_media`, `files`; `ReplySender.reply`
  and `PlatformReplies.reply` gain optional `session_key` / `message_ref`.
- Owner liveness uses `gateway.token_locks.pid_alive` / `process_start_time` until P15-24's
  `daemon/liveness.py` lands (one import to change; `DeliveryLedger(is_alive=…)` is injectable).

Compare checklist (filled after the rewrite):
(reopened `gateway/delivery_ledger.py`)
- [x] pending before any send, attempting just before the await, delivered only on success,
  failed on a refusal, abandoned (`tests/unit/state/test_delivery_ledger.py::test_record_and_walk_the_states`,
  `tests/unit/gateway/delivery/test_ledger.py::test_a_final_is_recorded_then_delivered`)
- [x] six recovery cases: pending → plain, attempting with a dead owner → labelled, failed →
  30 s / 120 s then reserved for boot, reconnect-only → reconnected label, flood → stated wait
  (default 60, cap 15 min, +2 s) with adoption and its own label, whole-chat death never retried
  (`test_ledger.py::test_boot_sweep_rules`, `::test_refused_send_retries_after_30_then_120_then_waits_for_boot`,
  `::test_reconnect_only_rows_wait_for_the_platform`, `::test_flood_waits_the_stated_time_capped_and_labels_the_retry`,
  `::test_flood_default_wait_without_a_stated_time`, `::test_whole_chat_death_is_abandoned_and_never_retried`)
- [x] three distinct prefixes, our wording (`DUPLICATE_PREFIX`, `RECONNECTED_PREFIX`, `FLOOD_PREFIX`)
- [x] owner liveness by pid + start time; claims compare-and-set on the previous owner
  (`test_delivery_ledger.py::test_claim_is_compare_and_set_on_the_owner`, `test_ledger.py::test_owner_alive_uses_the_start_fingerprint`)
- [x] platforms not connected at boot are left alone (no attempt spent) (`::test_boot_sweep_rules`)
- [x] max 3 attempts, 24 h stale, 7-day retention, 500-row cap delivered first
  (`test_delivery_ledger.py::test_the_501st_row_prunes_the_oldest_delivered_first`, `::test_retention_prunes_old_terminal_rows`,
  `test_ledger.py::test_rows_older_than_a_day_are_abandoned_at_boot`)
- [x] ledger errors never block a send (`test_ledger.py::test_a_ledger_failure_never_blocks_the_send`)
- [x] `gateway.delivery_ledger: false` disables (`::test_ledger_can_be_switched_off`)
- [x] kill points (`tests/integration/gateway/test_ledger_recovery.py::test_kill_point_then_boot`)
- [x] R06.5 marker after the ledger write (`tests/unit/gateway/delivery/test_turn_wiring.py::test_active_turn_marker_clears_only_after_the_final_is_in_the_ledger`)
- Deliberate differences: a row refused definitively is retried without a label (it cannot be a
  duplicate); stored text is masked; the undelivered pieces of a partly sent reply are stored and
  resent alone (the reference resends the whole text with a "part may have arrived" label);
  failure class and retry time are columns, not parsed from the error text; no multi-profile
  `adapter_profile` column (one store per profile, D-16.17).
- Not taken here: adopting a reply a killed process persisted but never ledgered (boot recovery,
  P15-26 owns turn persistence); releasing a runtime claim without spending an attempt (our claim
  happens only after the platform's sender is found; a sender vanishing in between spends one).
