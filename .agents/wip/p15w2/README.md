# P15 waves 2–3 — slice reports (base d0f29d0)

## W1 — w20-p15w1.patch — DONE (P15-19 delivery ledger, P15-20 deliverers/router/directory/mirror/`indus send`)
- New: state/delivery_ledger.py; gateway/delivery/{__init__,ledger,router,directory,mirror,send_command}.py;
  tests (state ledger table, gateway/delivery ×5, integration/gateway ledger kill-point recovery,
  cli test_send); notes delivery-ledger, platform-deliverers.
- Shared: state/schema.py delivery_ledger table; config gateway.delivery_ledger (default true) + test;
  outbound/sender.py (send_with_retry for every piece, ledger checkpoints, FinalReply fields),
  outbound/ports.py send_final_with_retry (plain fallback strips markup), outbound/replies.py (reply()
  session_key/message_ref; add/remove), core/admission.py (ledger key `<sid>:<turn_id>`, R06.5 gate,
  mirror notes staged per turn), adapters/base.py (ChannelEntry, list_channels → None), cli/app.py `send`
  row (string-loaded), contract help + non-TTY (send exit 2), core/_kit.py, test_sender partial-failure
  test, traps R06.5, R02.26; ACC-H10-1, K30-1.
- Daemon wiring (W3): ledger = DeliveryLedger.for_scope(scope, store) (None if disabled);
  OutboundSender(..., ledger=ledger) per platform; ledger.bind(platform_replies.sender_for); boot
  recovery `await ledger.recover()` before adapters accept turns (table.rows_for_session_key to avoid
  regenerating); on (re)connect `await ledger.platform_reconnected(name)` + router.attach(name, sender);
  fatal disconnect router.detach(name); shutdown ledger.aclose(), router.close(); router =
  DeliveryRouter(scope, homes=…, directory=ChannelDirectory(scope), mirror=Mirror(store, service));
  directory.start(lambda: adapters, store) / directory.stop(task).
- Decisions (need D-ids): only turn finals ledgered (cron keeps its own); no "delivery failed" notice when
  the ledger will retry; labels only when a duplicate is possible; ledger stores masked text; _finish
  waits ≤300 s for turn-end before clearing the marker; mirror notes via the note channel; `indus send
  --file` attaches; owner liveness via token_locks helpers until P15-24's liveness module.
- Follow-ups: `indus send` connects adapters for one send — P15-33..35 need a send-only open (Telegram
  long polling must not compete with a running daemon); a sender vanishing between claim and redelivery
  spends one attempt; GatewaySection merge with other slices.

## W4 — w20-p15w4.patch — DONE (P15-27 control socket, P15-28 service units + `indus gateway`, P15-29 reconnect watcher, /platform, gateway hooks)
- New: gateway/{control_socket,hooks}.py, gateway/daemon/{__init__ (docstring — W3 likely too), reconnect,
  side_services}.py, service/{gateway_control (wire format, socket paths, client), gateway_runtime (status
  file reader, planned-stop record, alive(pid, start_time), process_start_time), gateway_units (systemd /
  launchd generators), gateway_manager (systemctl / launchctl ops)}.py, cli/commands/gateway.py; tests
  (control socket, hooks, daemon/test_reconnect, service/test_units, cli test_gateway; _stubs_w4.py
  STAND-IN — delete), golden tests/fixtures/gateway/units/ ×6; notes ×3.
- Shared: cli/app.py `gateway` row; config agent.reconnect_attention_after 1800 (+test);
  core/admission.py (reconnect watcher attribute; post_gateway_admission after session_for() with
  session_id); core/commands.py (/platform covers reconnect-pending); traps R06.17, R06.18; ACC-K2-1,
  K32-1, K33-1.
- Security: socket <home>/gateway.sock mode 0600 under umask 0177, peer uid check (else forbidden), never
  TCP; long homes → private temp dir + 0600 pointer; clients refuse a group/world-writable home; 64 KiB
  request / 512 KiB answer limits, unknown keys rejected, 2 s timeout.
- Wiring (W3): RUN_TARGET "indus.gateway.daemon.process:run_command" in cli/commands/gateway.py (point
  at W3's command); daemon calls `await side_services.attach(daemon)` right after its pid lock and
  provides DaemonPorts (scope, pipeline, hook_bus, runtime_status(), control_handlers(),
  connect_platform(name) -> ConnectOutcome, set_platform_status(name, status|None)); ControlSocketError
  → failed startup; services.platform_failed(name, error, retryable=, lost=), services.started(platforms),
  services.stop(); W3 status writer / drain use gateway_runtime helpers (files gateway_status.json,
  gateway_planned_stop.json); daemon/liveness imports process_start_time / alive from gateway_runtime.
- Decisions (need D-ids): shared code in indus.service per the layer rule (phase file listed
  gateway/control_client.py, gateway/service/); Restart=on-failure + RestartPreventExitStatus=78; launchd
  /bin/sh wrapper maps 78→0; hashed unit names for non-default homes; TimeoutStopSec = max(60,
  max(restart_drain, cron_drain + 10) + 30) reading agent.restart_drain_timeout, agent.cron_drain_timeout,
  gateway.systemd_watchdog_seconds (W2/W3 rows); `install --system` only prints; stop records the
  planned stop before signalling, kills only with --force; drain / reload-plugins / pause-for-update
  answer `unsupported` until registered.
- Cron tick guard (P15-21): cron cannot import service — pass a probe (gateway_control.identify(home)) from
  the CLI, or move the wire module down a layer.
- Follow-ups: P17 session-ticket / describe via ControlServer.register; agent:step from post_tool_call.
