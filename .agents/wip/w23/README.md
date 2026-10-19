# Wave 23 (P16 E1-E3, P17 K1-K2) — parked builder patches, base 78be7b3

E1 (P16-01 HTTP listener kit) is being landed alone by the lead (D-16.350..354).

## w23-k2.patch: P17-05..07 (generation fences, event log + replay, request re-delivery) — done
- Builder checks: 131 own + broker tests; broad service/gateway/tui_bridge/protocol run 2001 passed / 23 skipped; ruff,
  pyright, lint-imports, tooling.checks ok. No new deps.
- Stand-in: tests/unit/service/_stubs_k2.py (K1 admissions) used by test_fences.py. Assumed K1 interface:
  `receipt(admission_id) -> Receipt|None` (admission_id, state queued/started/terminal/unknown, generation int once
  started); `cancel(admission_id) -> Receipt` (queued→terminal; claimed returns started+generation; terminal/unknown
  unchanged). `GenerationFences(admissions=...)`. Admission worker/host must call fences.begin at claim, adopt once the
  agent exists, release at settle.
- Shared edit: service/broker.py — ids `srq-<12 hex>`; optional `book: RequestBook` (answerable_kinds from viewers, offers
  via book, settle on answer/withdraw, `_undeliverable` → RequestUnsupported). test_broker asserts the new id shape.
- Wiring for P17-08/13/14: one EventLog per daemon (gateway.ready.replay_epoch = EventLog.epoch; log assigns seq);
  per session RequestBook(sid, emit=elog.append, generation=fences.running) → ServerRequestBroker(book=...);
  fences.bind_requests(sid, book, broker.answer); viewers RequestViewer.of(...); errors → book.refuse; events.since →
  book.reoffer; RPC maps StaleGeneration → stale_generation (P17-25).
- Decisions: protocol files untouched (P17-25); stop(session_id, admission_id); interrupt of a finished turn = stale with
  current None (reference answers success); since without epoch = gap; server_request.cancelled kept beside
  request.settled{withdrawn}; undeliverable → RequestUnsupported; evicted-session counters kept for daemon life.
- Follow-ups: redirect not fenced; batch clarify locks + per-actor respond auth (P17-13/14); hard-interrupt of subagents
  in P17-08 TurnControls; book.session_id must follow compaction child ids (P17-08).

## w23-k1.patch: P17-01..04 (runtime ledger, admissions, durable turn lease, liveness watchdog) — done
- Builder checks: state/service/integration-service/layers/errors/protocol/contract-gateway 1503 passed / 23 skipped;
  ruff, pyright, lint-imports, tooling.checks ok. No new deps. No stand-ins.
- Shared edits: state/schema.py (4 runtime_* tables + indexes; new turn_leases columns), state/deletion.py (retire
  sessions), foundation/errors.py (runtime.* codes), session_lock.py docstring (superseded, D-16.69), traps/ACC rows
  (ACC-A63-1..3, ACC-B25-1..2).
- Wiring left for P17-08/18/22: SessionService takes the durable lease (acquire with claim generation or None for --local;
  reload when HeldLease.waited; held.start(on_lost=...) → hard interrupt; release in finally); P17-08 binds the admission
  runner (ticket.bind, touch/observe_event, waiting=, stop_reason → turn.done); P17-22 drain gate + resolve_unknown.
- Decisions: operator discard settles `discarded`; session row need not exist before admit; lease timeout → failed
  `lease_timeout`; `stalled` carried as failure reason of interrupted_by_system; retirement markers never pruned; lease
  timings are constants.
- Follow-ups: runtime.* codes onto protocol table (P17-25); backups still include ledger tables (R03.21 says exclude);
  rehoming queued input on automatic reset; a worker meeting a refusing gate exits (drain-ender restarts it).
