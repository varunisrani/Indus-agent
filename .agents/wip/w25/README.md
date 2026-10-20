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

## w25-e7.patch: P16-13 Google Chat + P16-20 QQ Bot — done
- Builder checks: adapters+contract+schema+config 788 passed / 43 skipped; ruff, pyright, lint-imports, tooling.checks ok.
  RS256 via `cryptography` (lazy import); no hand-rolled crypto. Fixed while building: a 401 send read as "chat not found";
  media host check by suffix → exact Google host list.
- Extras: `google-chat = ["cryptography==50.0.2"]`, `qqbot = ["httpx-ws==0.9.0"]` (both already locked) — re-lock metadata.
- Wiring (core): registry rows `_chat_entry("google_chat", "Google Chat", sdk=("cryptography",), extra_name="google-chat",
  required_env=("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON",), max_len=4000, hint="You are talking through Google Chat. *bold*,
  _italic_, ~strike~ and code render; headings and tables do not.")` and `_chat_entry("qqbot", "QQ Bot", sdk=("httpx_ws",),
  extra_name="qqbot", required_env=("QQ_APP_ID", "QQ_CLIENT_SECRET"), max_len=4000, hint="You are talking through QQ.
  Formatting is not rendered, so write plain text.")`; BUILTIN_NAMES += google_chat (+ google_chat_pubsub case?), qqbot;
  BUILTIN_CREDENTIALS google_chat: (GOOGLE_CHAT_SERVICE_ACCOUNT_JSON,), qqbot: (QQ_APP_ID, QQ_CLIENT_SECRET).
  Drain probe: daemon calls adapter.set_drain_probe(daemon.lifecycle.notice) on platform up (better: a base Adapter seam).
  `indus send` tested via send-only connect only — add the send_command test once registry rows exist.
- Decisions: own clients (no Google/QQ SDK); RS256 only, keys via injectable fetcher (300 s cache, 1 forced refetch/min),
  both audience flavours (project number / endpoint URL + verified email); service account inline JSON from SecretStore
  only, non-Google token_uri refused; listener 127.0.0.1:8647 /google-chat, 1 MiB; no typing/media upload (clean refusal);
  card clicks handed to core; QQ chat ids prefixed group:/channel:/dm:; passive-reply window 300 s; markdown off; heartbeat
  80 % with missing-ack socket replacement; AT_MESSAGE_CREATE handled.
- LEAD RULING: a test sets `data["private_key"] = "-----BEGIN PRIVATE KEY-----\nAAAA..."` as a literal — build the PEM
  armour at runtime (or generate a real throwaway key with cryptography) so no credential-shaped literal is in the tree.
- Follow-ups: base drain-probe/ingress-gate seam; Google Chat native uploads (per-user OAuth); QQ webhook mode;
  commandId-only slash commands.

## w25-e8.patch: P16-16 DingTalk + P16-17 WeCom smart robot + P16-18 WeCom callback — done
- Builder checks: dingtalk/wecom/contract/schema 310 passed / 41 skipped; config/registry/adapters 560 passed; ruff,
  pyright, lint-imports, tooling.checks ok. AES via `cryptography` Cipher (lead-checked); XML via stdlib expat with DTD,
  entity declarations and external refs refused (lead-checked). Crypto vectors from openssl/coreutils (script + json,
  re-run by a test). Own DingTalk Stream client (dingtalk-stream MIT but not used).
- Extras: `dingtalk = ["httpx-ws==0.9.0"]`, `wecom = ["httpx-ws==0.9.0", "cryptography==50.0.2"]` (already locked) — re-lock
  metadata; 00-layout §4 pin rows added. Note: cryptography 50.0.2 upload is 4 days before exclude-newer (pins check passes).
- Shared: contract `lacks` shapes voice_media, document, caption; conftest 3 imports; schema rows platforms.dingtalk /
  wecom / wecom_callback; traps R06.12.
- Wiring (core): BUILTIN_NAMES += dingtalk, wecom, wecom_callback; registry rows
  `_chat_entry("dingtalk", "DingTalk", sdk=("httpx_ws",), extra_name="dingtalk", required_env=("DINGTALK_CLIENT_ID",
  "DINGTALK_CLIENT_SECRET"), max_len=20000, hint=…)`, `_chat_entry("wecom", "WeCom", sdk=("httpx_ws",), extra_name="wecom",
  required_env=("WECOM_BOT_ID","WECOM_SECRET"), max_len=4000, hint=…)`, `replace(_chat_entry("wecom_callback", "WeCom
  (callback app)", sdk=("cryptography",), extra_name="wecom", required_env=("WECOM_CALLBACK_CORP_ID",
  "WECOM_CALLBACK_CORP_SECRET","WECOM_CALLBACK_AGENT_ID","WECOM_CALLBACK_TOKEN","WECOM_CALLBACK_ENCODING_AES_KEY"),
  max_len=2048, hint=…), factory=_lazy_factory("indus.gateway.adapters.wecom:create_callback_adapter"))` (hints in the
  builder notes); BUILTIN_CREDENTIALS dingtalk (CLIENT_ID, CLIENT_SECRET), wecom (BOT_ID, SECRET), wecom_callback (CORP_ID,
  CORP_SECRET, TOKEN, ENCODING_AES_KEY); lock_env on stable ids (e.g. dingtalk CLIENT_ID, wecom BOT_ID, wecom_callback
  CORP_ID + AGENT_ID). streaming_default("wecom") True once P16-33 lands.
- Decisions: stdlib expat (hardened) instead of defusedxml (not locked); callback text cap 2,048 bytes; port 8645 clashes
  with BlueBubbles default (doctor P16-36); constant-time signatures + ±300 s replay window; secrets added inside the
  transport (never in logs/response.request); DingTalk REST fallback rules; WeCom message ids from req_id; takeover
  reconnect once after 60 s, second within 10 min fatal (wecom_replaced); 30 msgs/min/chat window; dm_policy pairing.
- Follow-ups: ACC-K11 rows missing; WeCom inbound media has no private-address guard (add one at integration if cheap);
  split-message batching; P16-32 target parsers.
