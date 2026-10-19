# P14 cron — slice reports (base adaceac)

## C3 — w17-p14c3.patch — DONE (P14-13..16, 18, 19 delivery, bookkeeping, incidents, monitor, cronjob_manage, CLI/`/cron`/status/doctor)
- New: cron/{ports,targets,delivery,bookkeeping,incidents,monitor,tool,doctor}.py, cli/commands/cron.py,
  cli/slash/cron_cmds.py, tests/unit/cron/* (_stubs_c3.py STAND-IN: StubJob + in-memory backend —
  delete), tests/unit/cli/commands/test_cron.py, notes cron-{delivery,bookkeeping,incidents,monitor,tool,cli}.md.
- Shared: config CronSection with C3 rows (wrap_response, local_notify, failure_nudge_threshold,
  retry_unreachable, failure_repeat_alert_hours, allow_agent_scheduling) — UNION with C1/C2 rows;
  tools/toolsets.py (`cronjob` toolset; cronjob_manage in CORE_TOOLS and DEFERRED_BY_DEFAULT);
  test_toolsets, test_wizard (cronjob row); cli/runtime.py _tool_registry registers the cron tool;
  service/session_service.py per-session cron result sink (deliver_internal), undone at close;
  cli/slash/dispatch.py, test_registry (cron landed, help group); traps R07.17, R07.15, R01.11;
  ACC-H10-1, H15-1, H16-1.
- Wiring: ports.BACKEND_FACTORY = adapter over C1 store/ledger/notepad + C2 ticker (CronBackend
  protocol in ports.py); add `cron` to cli/app.py REGISTRY + non-TTY/help rows (listed in cron-cli
  note; decide how `cron daemon` fits the 5 s contract watchdog); C2 runner order:
  monitor.check_monitor (monitor.RUN_SCRIPT = C2 script runner) → delivery.deliver → inside C1's
  locked update bookkeeping.settle_run (append nudge, hold_notice; notice only when notify) →
  incidents.decide_failure_alert / resolve_after_success; status reads <home>/cron/ticker_heartbeat
  and ticker_last_success by mtime (C2 writes them); indus-cron bundle drops cronjob_manage unless
  allow_agent_scheduling; tool._SCAN_SCOPE → C2's `cron` threat-scan scope.
- Decisions: CronJob gains scheduler-owned `unreachable_retry {attempt, at, expr}`; doctor reads optional
  `last_dispatch {kind, lateness_seconds}`; agent-supplied inference pins refused; restart-loop guard
  refuses `indus … gateway restart|stop` (sibling profiles too); monitor body >256 KiB is an error;
  bare `indus cron` prints help; notepad verb `unset`; "unreachable" = P03 connection reason without
  HTTP status, quota hold = rate_limit/billing with reset_at/retry_after and zero answered calls;
  known platforms = P15/P16 adapter names (+ register_platform); incident ledger has its own SQLite
  connection (switch to C1's helper).
- Delivery attachments only as Path inside <home>/cron/output/<job>/ passing P05 read_denial; never
  from reply/model/MCP text.
- Follow-up: result-sink registration does not follow a rotating compaction's new session id (run
  falls back inline).
