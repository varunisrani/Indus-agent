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
