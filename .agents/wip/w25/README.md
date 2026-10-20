# Wave 25 (P17 K3/K4, P16 E6/E7) — parked builder patches, base d50d5d3

## w25-k4.patch: P17-10 tickets + ws_auth, P17-11 attach verbs, P17-12 shared RPC dispatcher — done
- Builder checks: service/daemon/control/tui_bridge/redact/contract 772 passed / 39 skipped; neighbours 175 passed; ruff,
  pyright, lint-imports, tooling.checks ok. No new deps.
- Stand-in: tests/unit/gateway/daemon/_stubs_k4.py (K3 host). Assumed `AttachHost`: `creatable_sources() -> Sequence[str]`,
  `capabilities() -> Sequence[str]` (sync reads; no await/lease/store). Swap StubHost in test_attach_verbs.py for the real
  host and delete the stub at integration.
- Shared edits: gateway/control_socket.py (KNOWN_VERBS, OWNER_ONLY_VERBS by name, register(owner_only=),
  handle_line(peer_verified=), VerbRefused, public code_version); service/gateway_control.py (session_ticket, describe,
  TicketReply hides ticket, ws_url_is_safe); foundation/redact.py (+test: masks indus-gateway-ticket.<t>, `ticket` query);
  P12 tui_bridge dispatch/stdio_server/bridge moved onto service/rpc (thin layer).
- Wiring owed: daemon start builds TicketBook.for_home + AttachVerbs(host=DaemonSessionHost, ws_url=...), merges
  attach.handlers() into DaemonBridge.control_handlers(), attach.close() at stop, gateway.ready carries profile_id/
  instance_id; P17-13 authenticate_upgrade + Dispatcher loop + check_event; P17-15/16/19 clients describe → session_ticket
  per dial; P18-06 purposes; P17-25 error mapping (ticket_invalid, deadline code, protocol_version placement).
- Decisions: any failed redemption spends a known ticket (reference keeps it on wrong purpose); non-loopback/no-address
  peer → 4403; profile_id/instance_id optional; no forwarded subject/host scope (P24); unsafe home refused but symlinked home
  allowed; dispatcher deadlines 120 s default, prompt.submit/background 1,800 s, session.compress 600 s.
- Lead note for the integrator: make the redaction of `indus-gateway-ticket.<t>` a FULL mask (today keeps first/last 4).

## w25-k3.patch: P17-08 daemon session host + P17-09 frozen creation policy — done
- Builder checks: service/contract-gateway/layers/foundation 1123 passed / 41 skipped; test_daemon_host 5x green; wider
  1821 passed with 5 load-only failures in tests/integration/core/test_d04_1_terminal_*backstop.py (pass alone) — ROOT-CAUSE
  at integration; ruff, pyright, lint-imports, tooling.checks ok. No new deps.
- Interface (full signatures in docs/notes/P17/daemon-host.md): DaemonSessionHost.open(service, store, instance_id=, leases=,
  liveness=, gate=, on_blocked=, event_log=, offload=); create/resume/attach/detach/detach_connection/submit/receipt/wait/
  discard_unknown/interrupt/steer/respond/refuse/stop/events_since/state/key_of/close_session/aclose; Viewer.of(...).
- Shared edits: service/session_service.py (broker_factory, turn_door, start_admitted, reload, interrupt(by_system=),
  in-process events_since/receipt); service/admissions.py (TurnTicket.lease_waited); foundation/errors.py
  (runtime.policy_conflict, runtime.policy_refused); traps R04.1, R05.18.
- Wave-1 wiring done: ticket.bind, stop-before-bind, reload on lease_waited, hard interrupt stops subagents, one EventLog +
  run_flusher + replay_epoch, per-session RequestBook + broker + fences.bind_requests, forget on close, book.session_id
  follows compaction.
- Decisions: host keys a session by its first attached id across compactions; reload closes the old agent (its cleanups
  run); P02 file lock stays beside the lease until P17-18; create writes the row only with a policy; host owns the service;
  frozen fields beyond cwd/model/toolsets applied by the daemon's agent factory; safe-mode needs provider+model; unknown
  launch fields refused; never-frozen field = default (naming it on resume conflicts); subscriptions keyed by connection id.
- Follow-ups: per-actor authorisation + slow-viewer overflow (P17-13/14); alias resolution, refused-flag hints, close reasons
  (P17-18); policy_conflict protocol mapping (P17-25); unviewed ACP sessions unloaded not ended at shutdown.
