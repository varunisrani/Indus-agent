# Wave 23 (P16 E1-E3, P17 K1-K2) — parked builder patches, base 78be7b3

E1 (P16-01 HTTP listener kit) LANDED e3b3aa1 (D-16.350..354).

## (LANDED ff7bbe3) w23-k2.patch: P17-05..07 (generation fences, event log + replay, request re-delivery) — done
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

## (LANDED ff7bbe3) w23-k1.patch: P17-01..04 (runtime ledger, admissions, durable turn lease, liveness watchdog) — done
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

## w23-e2.patch: P16-06 Matrix (plain) + P16-08 Mattermost — done
- Builder checks: own + contract 148 passed / 25 skipped; wider gateway/cli/foundation/integration-gateway 2721 passed;
  ruff, pyright, lint-imports, tooling.checks ok. Own Matrix C-S client over httpx (no mautrix; MPL left for P16-07 E2EE).
- Extra `mattermost = ["httpx-ws==0.9.0"]` (MIT; httpx-ws + wsproto already locked via daytona) — re-lock (metadata only).
- Shared: contract conftest `_register_p16_cases()`; config schema rows platforms.matrix / platforms.mattermost (+ test).
- Wiring (core): registry rows `_chat_entry("matrix", ..., sdk=("httpx",), extra_name="matrix", required_env=(MATRIX_ACCESS_TOKEN,
  MATRIX_HOMESERVER), max_len=15000)` and mattermost (sdk httpx_ws, required_env MATTERMOST_TOKEN, MATTERMOST_URL, max_len 4000);
  BUILTIN_CREDENTIALS matrix: (MATRIX_ACCESS_TOKEN,), mattermost: (MATTERMOST_TOKEN,) (URLs not masked); daemon hooks
  register_fetchers, lost_handler(reason, fatal) → disconnect_platform + reconnect watcher, processing reactions.
  Send-only: inbound starts only once set_sink installs a sink (no change needed). P16-32: register matrix.split_target.
- Decisions: auto_thread off by default; m.notice → bot-marked events; sync backoff 2→60 s; Mattermost token only in upgrade
  header; @channel/@all/@here defused; e2ee optional/required seam (required fails fatal until P16-07).

## w23-e3.patch: P16-05 Signal + P16-09 Email — done
- Builder checks: signal 31, email 93, own+contract 218 passed / 23 skipped, unit/gateway 995 passed; ruff, pyright,
  lint-imports, tooling.checks ok. No new packages (signal-cli GPL-3.0 only as the user's own separate process; email stdlib).
- Shared (review): contract conftest `AdapterCase.lacks` + case imports; test_adapter_contract honours `lacks`, `_long_text`
  linear; test_vendor_names adds EmailWire; traps R06.3, R06.8; ACC-K8-1.
- Wiring (core): registry rows signal (probe httpx, required_env SIGNAL_HTTP_URL, SIGNAL_ACCOUNT, max_len 8000,
  group_chats_env INDUS_SIGNAL_GROUP_ALLOWED_CHATS) and email (stdlib probe, required_env EMAIL_ADDRESS, EMAIL_PASSWORD,
  EMAIL_IMAP_HOST, EMAIL_SMTP_HOST, max_len 50000) + BUILTIN_CREDENTIALS; send-only: `adapter.inbound_enabled = False`
  before connect (reconcile with wave-22's AdapterConfig.send_only); register_fetcher(name, adapter.fetch_attachment);
  token-lock key SIGNAL_ACCOUNT / address+IMAP host.
- Decisions: plain text only; no client-side attachment rate model; Signal groups need a mention unless require_mention
  false; email replies over 50,000 split into threaded mails; empty mail ignored; X-Auto-Response-Suppress alone not dropped;
  From trusted only via topmost Authentication-Results with pinned authserv-id + DMARC/aligned SPF/DKIM.
- Follow-ups: R06.8 core order (adapter DM policy before global unauthorized_dm_behavior); Adapter.report_failure hook;
  `lacks` into Capabilities; allowlists compare exactly (lower-case emails).
