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

## w25-e6.patch: P16-11 Microsoft Teams + P16-12 Graph change-notification receiver — done
- Builder checks: teams+msgraph 106 passed; contract+config 388 passed / 41 skipped; (pre-restart) unit/gateway+contract+
  config 1747 passed; ruff, pyright, lint-imports, tooling.checks ok. No new packages.
- New shared helper gateway/adapters/_entra.py (client-credentials tokens, cached, single-flight, secrets out of reprs).
- Wiring (core): registry rows
  `replace(_chat_entry("teams", "Microsoft Teams", sdk=("httpx",), extra_name="teams", required_env=("TEAMS_CLIENT_ID",
  "TEAMS_CLIENT_SECRET", "TEAMS_TENANT_ID"), max_len=28000, hint="You are talking through Microsoft Teams. Markdown is
  rendered (bold, italic, code, lists); avoid tables and raw HTML. Keep replies clear and professional."),
  install_hint="pip install indus-agent (Teams needs no extra)")` and
  `PlatformEntry(name="msgraph", label="Microsoft Graph change notifications", factory=_lazy_factory(
  "indus.gateway.adapters.msgraph:create_adapter"), check_fn=modules_installed("httpx"),
  required_env=("MSGRAPH_WEBHOOK_CLIENT_STATE",), install_hint="pip install indus-agent (no extra)", dm_is_private=False,
  trusted_inbound=True)`; BUILTIN_NAMES += teams, msgraph; msgraph in send_command._NOT_TARGETS;
  BUILTIN_CREDENTIALS teams: (CLIENT_ID, CLIENT_SECRET, TENANT_ID), msgraph: (MSGRAPH_WEBHOOK_CLIENT_STATE,).
  Daemon: GraphReceiver.set_forward(...) + platforms.msgraph.deliver_to.
- LEAD RULINGS for the integrator:
  1. Replace the hand-rolled RS256 signature verification in teams/botauth.py with `cryptography`'s RSA PKCS#1 v1.5
     verify (cryptography is a base dependency). Keep our own claim checks (RS256 only, iss, aud, exp/nbf skew, kid
     cache/refresh, key size >= 2048, endorsements, serviceUrl). No custom crypto math in src/.
  2. Graph replies with no forward configured must NOT be reported as delivered: return a typed non-delivered result
     (e.g. not_configured / undeliverable) and log once.
- Decisions: Bot Framework issuer only (any tenant); refs/subscriptions in 0600 JSON under <home>/gateway/; card presses go
  to the core requester/admin rule; platform name msgraph; clientState 16–128 printable ASCII; Graph events internal and
  allow_control False; service-URL allow-list refuses lookalikes/ports/http/userinfo/IPs; validation token echo capped 1,024.
- Follow-ups: P16-32 teams target parser; allowed_tenants option; proactive DM to new users; non-image files (file
  consent); E7 may reuse botauth/_entra; Teams meetings (K12, D-16.87); ACC-K12 rows missing.
