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

## C1 — w17-p14c1.patch — DONE (P14-01..06, 17: grammar, next run, job model/store, ledger, store health, due/catch-up, notepad); uv.lock EXCLUDED
- New: cron/{errors,zones,grammar,next_run,models,store,ledger,notepad,store_health,due,context}.py,
  service/cron_refs.py (curator adapter — layer rule: cron may not import learning), tests/unit/cron/*,
  tests/fakes/pids.py, tests/fixtures/cron/jobs_{valid,corrupt,legacy_list}.json, notes ×7.
  The model/store API (and the ticker call order gate → reap → evaluate_all → patch repairs →
  claim + patch(updates) → dispatch) is the first section of docs/notes/P14/cron-store.md.
- Shared: pyproject croniter==6.2.4 (pulls python-dateutil, six; licence check passes) — RE-LOCK;
  config CronSection with cron.catch_up_missed (UNION with C2/C3 rows) + test; traps R07.8–R07.13;
  ACC-H1-1, H2-1, H2-2 (new, DST), H3-1, H13-1.
- Wiring: register_cron_references(CronStore(scope)) from service/host_inprocess.py at host start;
  add cron.* codes (cron/errors.CRON_ERROR_CODES) to foundation/errors.ERROR_CODES; C2's script-path
  rule into CronStore(script_check=…) (CronContext.open passes it); C3's bookkeeping MUST call
  plan_next after setting last_run_at (steps from last_dispatch.scheduled_for; following_slot) —
  otherwise a late run in the repeated fall-back hour schedules 01:30 twice. C3's CronJob needs
  `unreachable_retry` (C3 decision) — add to C1's model.
- Decisions (need D-ids): every Nd at HH:MM — N=1 daily cron, N=7 weekly cron, else anchored interval
  in UTC (wall clock shifts across DST); exactly five cron fields; canonical display strings that
  parse back; spring-forward gap fires at gap end (03:00); one run per slot (any attempt state blocks;
  retries kind="retry"); 500 finished attempts kept per job; reaper never touches own process, 2 h
  floor with inactivity 0; unknown jobs.json shape refused and left on disk, dropped records in
  jobs.rejected.json; scheduler fields schedule_error, last_dispatch; unknown keys preserved;
  store-health notices per process; notepad keys no control chars; no hypothesis (seeded tables).
- Note: this host's SQLite has the WAL-reset bug → executions.db opens in DELETE mode (P02 policy).
