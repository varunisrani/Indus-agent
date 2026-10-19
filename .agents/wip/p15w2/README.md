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

## W3 — w20-p15w3.patch — DONE (P15-24 daemon process, P15-25 drain/restart/shutdown/watchdogs, P15-26 boot recovery)
- New: state/gateway_runtime.py (process identity pid+start time+pid ns, zombies gone; pid record;
  RuntimeStatus writer/reader + 120 s stale; planned-stop record; exit codes 0/75/78;
  INDUS_GATEWAY_SUPERVISED, _INDUS_GATEWAY); gateway/daemon/{__init__,parts (DaemonPart, Stage,
  PART_FACTORIES),liveness (flock, claim_instance, --replace),status,process (GatewayDaemon,
  DaemonOptions, AdapterConfig, build_service, run_daemon, run_command),command,lifecycle,drain,watchdogs,
  recovery}.py; notes daemon-process (API), daemon-lifecycle, boot-recovery; tests (unit daemon ×3,
  integration test_daemon_start / test_signals / test_boot_recovery, kits daemon_kit + spool, fixture
  spool_plugin, _stubs_w3.py STAND-IN for `indus gateway run` — delete when W4 lands).
- Shared: gateway/token_locks.py (helpers from state.gateway_runtime, re-exported); gateway/__init__.py
  lazy exports; config gateway.connect_timeout_seconds 30, gateway.systemd_watchdog_seconds 0,
  agent.restart_drain_timeout 0, agent.restart_after_turn_timeout 1800 (+test); traps R03.20/21,
  R06.16–20; ACC-K1-1, K3-1, K31-1.
- Plug-in points (API in docs/notes/P15/daemon-process.md): slices add "module:attr" factories to
  PART_FACTORIES; in prepare: W2 sets pipeline.on_request / interceptors / enrich; W1 sets
  daemon.sender_options[...] (ledger) and daemon.recovery.crash_reply_sink, registers P14 deliverers via
  on_platform_up; stages CONTROL (W4 control socket, required, stops last), RECOVERY (W1 ledger sweep),
  BACKGROUND (W2 cron ticker, W4 reconnect watcher, W1 channel directory).
- W3/W4 MISMATCHES — LEAD RULINGS for integration: (1) one runtime-records module: KEEP
  state/gateway_runtime.py (cron sits below service), fold W4's service/gateway_runtime.py into it;
  (2) status file name gateway_state.json (already excluded by `indus import`); W4 reads that;
  (3) W4's side_services.attach becomes a required CONTROL DaemonPart (attach in start; platform_failed
  via on_platform_down; started once platforms up; stop in stop); (4) connect_platform returns
  PlatformRuntime — adapt W4; (5) W4 builds control handlers from identity(), status_snapshot(),
  request_stop / request_restart, lifecycle.set_draining / clear_draining, plugins;
  (6) agent.cron_drain_timeout belongs to W2.
- Decisions (need D-ids): status file doubles as unclean-death marker; `degraded` = parked fatal platform
  or loop-watchdog exit (exit_reason distinguishes); exit 78 only when nothing connected, ≥1 connect-time
  conflict, nothing retrying (missing library / unbuildable adapter parks but never forces 78); failed
  required part or unexpected startup error exits 75; a crash-interrupted turn is paused, not resumed
  (R06.19; reference resumes once); _INDUS_GATEWAY via scope env + child_env _INDUS_FORCE_; concurrency cap
  read at start; adapter factories get AdapterConfig(name, settings, scope, clock, proxy) — P15-33..35
  implement create_adapter(config).
- Follow-ups: daemon does not yet feed StreamConsumer / ToolProgress / typing / heartbeat (replies go out
  whole); chat sessions lack the learning loop (with_learning lives in cli/runtime.py — move to service/);
  process tests to call `python -m indus gateway run` after W4 merges; ACC-K1-2..4, K34-1 rows missing.

## W2 — w20-p15w2.patch — DONE (P15-21 cron ticker in the daemon, P15-22 approvals/clarify over chat, P15-23 intake)
- New: gateway/{cron_host,extras (install_extras — one registration function)}.py, gateway/core/prompts.py,
  gateway/intake/{__init__,media,voice}.py, service/approval_copy.py; tests (unit gateway cron_host,
  cron_world, extras, core/test_prompts, intake ×2, integration test_cron_delivery); notes ×3; STAND-INS
  tests/unit/gateway/_stubs_w2.py (StubDaemon) and tests/integration/gateway/_stubs_w2.py (home-channel
  deliverer) — delete.
- Shared: cron/ticker.py (lease functions, Ticker(hosted=), "deferred" status); core/admission.py
  front_doors hook (after pause/restore, before control routing/busy/mention); config gateway.stt_enabled
  (+test); traps R02.5, R05.22, R06.1, R06.6, R06.23, R07.17; ACC-E8-1, H3-1, K24-1;
  tests/integration/gateway/__init__.py (empty; W1 has one too).
- Wiring (W3): `indus.gateway.extras.install_extras(daemon, cron=True)` once AdmissionPipeline is built,
  before accepting events; needs daemon.scope, daemon.pipeline, daemon.on_start/on_stop hooks (map onto
  W3's DaemonPart/stages); sets pipeline.enrich, pipeline.on_request, album collector + prompts as front
  doors, starts/stops GatewayCronHost; adapters register downloads via
  extras.intake.register_fetcher(platform, fn); buttons as metadata["button"].
- SECURITY — fix at integration: a typed `/approve` sent straight to pipeline.handle() (bypassing
  accept()) skips the requester check and falls back to G4's handler — make G4's /approve and /deny
  handlers enforce the same requester-or-admin rule (and bots never answer), with a test.
- Decisions (need D-ids): D-16.230 — no PluginUpkeep for cron runs; ticker ownership via lease file
  <home>/cron/ticker_owner.json (stale 180 s / dead / reused pid; deferred tick exits 0) instead of a
  control-socket probe (layer rule); requester-or-admin, admin only when an admin list is configured;
  gateway denies only with no request deadline or broker 2 s past its own (R02.5); "native" image mode
  is a note pointing at vision_analyze (submit is text-only — real image parts need a P04 submit seam);
  album window 0.8 s quiet / 3 s cap; narration-of-silence settles as silent.
- Follow-ups: CLI/TUI approval cards onto service/approval_copy.py; cache age cleanup in daemon
  housekeeping; Docker path translation for cached files; cron ledger attempt shows ok while the job
  shows silent.

## w20-p15w5.patch: P15-30..32 (API server, runs/jobs API, webhooks), base d0f29d0
- Builder checks: 3227 passed / 25 skipped on gateway/state/cli/config/contract-cli; ruff, pyright, lint-imports, tooling.checks ok.
- New core deps fastapi==0.142.2 (+ annotated-doc 0.0.5) and uvicorn==0.54.0: re-lock (uv.lock excluded from the patch).
- Most of the size is `tests/fixtures/gateway/openai_schema/` (trimmed openai/openai-openapi @ 33248a6, MIT, SOURCE.md).
- Stand-in: `tests/unit/gateway/http/_stubs_w5.py` (daemon). Wiring:
  - W3 daemon: `surfaces = await start_http_surfaces(HttpHost(scope, services=factory, store=handle))`; factory builds one
    SessionService per SurfaceSpec (WEBHOOK_SURFACE untrusted: no MCP/plugin toolsets); `HttpHost.on_status` gets API counters
    every 30 s; `surfaces.problems` lists failed surfaces; `await surfaces.aclose()` before closing services.
  - W4: `gateway_group.add_command(api_key_group)` (indus.cli.commands.gateway_api_key).
  - cli/app.py `webhook` row is also in W4 (P15-28 owns it): keep one copy.
  - Swap `CompletionLog` (`<home>/gateway/api_completions.jsonl`) for W1's delivery ledger.
- Decisions: config under gateway.api_server / gateway.webhook; auth on /health too and no key = refuse to start; no session
  header = new session with system+history folded into first message; data-URL images cached under
  `<home>/cache/api_server/images`; failures keep OpenAI finish_reason plus an `indus` error object; runs/idempotency/stored
  responses in memory; webhook route secrets are secret-store names; generic headers X-Indus-Signature / -Timestamp /
  -Signature-Ts; failed direct delivery forgets its delivery id; coalesced events dropped at shutdown; /api/cron/fire guarded
  by the API key.
- Follow-ups: reference routes with no phase row (/health/detailed, /v1/capabilities, /api/model/options, /v1/skills,
  /v1/toolsets, session create/patch/delete/messages/fork/chat); background /api/jobs/{id}/run not cancelled at shutdown.
