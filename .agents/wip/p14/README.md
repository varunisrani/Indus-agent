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

## C2 — w17-p14c2.patch — DONE (P14-07..12 ticker, preflight, script runner + wake gate, prompt assembly, agent runner, classification + output)
- New: cron/{ports,settings,jobroute,classify,output,script,prompt,preflight,runner,ticker}.py
  (`ports.py` CLASHES with C3's — merge into one module: C2's set_ports_factory/spec_of + C3's
  CronBackend/BACKEND_FACTORY), service/cron_host.py (agent run, skill loader/check — layer rule),
  tests/unit/cron/* (_stubs_c2.py STAND-IN — delete), tests/integration/cron/{tick_entry.py (delete),
  test_tick_e2e.py (point at `indus cron tick`)}, fixtures cron/scripts/*, golden cron/prompt_assembly.txt,
  notes ×6.
- Shared: config CronSection with C2's 10 rows (UNION with C1/C3; allow_agent_scheduling is in both C2
  and C3 — keep one); safety/threat_scan.py `cron` scope; tools/toolsets.py indus-cron bundle drops
  `clarify` (+ test_toolsets, contract test_reachability, golden cron/tools.json, P04 golden note);
  traps R07.13–16, R06.14 (+R07.5, R02.22 lines; C1 also edits R07.13); ACC-H4-1, H5-1, H8-1, H9-1,
  H3-2 (new), E7-1 line.
- Wiring: C3's CLI calls cron_host.tick_main / daemon_main / run_now, ticker.pause_all / resume_all;
  script.check_script_value for create-time refusal (and C1's CronStore(script_check=…));
  ports.set_ports_factory(...) registered once in cron_host over C1's store/ledger/due (fire_claim
  {owner, claimed_at, heartbeat_at}; next_run_at set at claim via C1's next-run); C3's cronjob_manage
  via CronAgentRunner(register_tools=…), denied unless cron.allow_agent_scheduling; PreflightProbes.mcp
  onto P13's McpHost.status(); _bind_project_trust → project_trust.bind_project_cwd; preflight
  .platforms_of → C3's target parser.
- Decisions (need D-ids): own tokens [JOB_FAILED], wake/context gate keys; silence token only alone;
  failing pre-run script fails without a model call; empty successful script skips the run;
  preflight alerts once per reason in our own file; `all` target blocked until P15; runner over
  core.agent.Agent (own session-id shape, session lock taken); inactivity via agent.turn_liveness_s;
  two-tier prompt scan (author text strict `cron`, runtime data `context` after invisible-char strip);
  global pause marker file.
- Follow-ups: expand skill bundles in a job's skills; merge job MCP servers into explicit toolsets;
  cron_* model-script fixtures unused; manual run on a paused job runs without resuming — confirm.
