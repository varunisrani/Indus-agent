# P14 · Cron

**Weeks:** 1.5 · **Track:** B (backend, Python) · **Depends on:** P09 (skill invocation used by jobs, `SkillReferenceSource` registry for curator protection, review skip flag), P08 (fallback chain, credential pools, auxiliary client P08-17), P05 (approvals with unattended policies, env builder, redaction, SSRF policy, threat scan), P04 (`SessionService` in-process host, prompt builder with the `cron` surface), P02 (store), P01 (config, clock, timezone, `SecretStore`) · **Status:** planned

## Goal
A person or the agent can schedule work in plain language ("every 30m check the build", "weekdays at
9am summarise my inbox", "in 2h remind me") and Indus runs it as a fresh agent session or a
script-only job, at most once per slot, with preflight checks that cost no tokens, silence and
failure tokens, failure streaks and incidents, and the output saved locally. Before the gateway
exists (P15), jobs fire from `indus cron tick` (for a user timer) or the `indus cron daemon` loop,
and delivery is local (files under `<home>/cron/output/`); P15 moves the ticker into the gateway
daemon and adds platform delivery through the same target grammar. This completes milestone M4:
"every 30m check the build schedules and runs a cron job with local delivery", with the
at-most-once and DST tests.

Not in this phase: platform delivery, `all`, home channels, continuable deliveries and the main-chat
target (H10 platform half, H11, H19 → P15/P16 through the deliverer registry defined here);
restart-safe worker processes under a service manager (H14 → P15); suggestions and blueprints
(H18, P3 → proposed for P24); the hosted scheduler provider (H20, dropped, P3).

Interop note: 5-field cron syntax (0 = Sunday, named weekdays and months, `*/n`, ranges, lists) is
the de-facto standard implemented by `croniter`; pin it in `00-layout-and-conventions.md` §4 and
check expressions against its documentation for that version. IANA zone handling uses the stdlib
`zoneinfo`. Read the reference only for behaviour.

## Tasks (19)
| ID | Task | Read in reference | New files | Verify |
|---|---|---|---|---|
| P14-01 | **Schedule grammar (P6, D-16.36, D-16.54).** `parse_schedule(text, tz) -> Schedule` where `Schedule = Once{run_at} \| Interval{minutes} \| Cron{expr}` plus a display string. Order: (1) natural day/time phrase with or without a leading `every`: day spec (`day\|daily\|everyday` → all; `weekday(s)` → Mon–Fri; `weekend(s)` → Sat+Sun; a list of weekday names or 3-letter abbreviations joined by spaces, commas or `and`), optional `at`, then a time (`9am`, `9:30pm`, `14:00`, bare 24 h hour `7`, `noon`/`midday`, `midnight`) → 5-field cron; (2) **`every Nd at HH:MM`** → daily (N = 1) or every N days at that wall-clock time (D-16.54; N > 1 compiles to a cron with day-of-month step only when it divides evenly, else an `Interval` anchored to the next occurrence of HH:MM; decision recorded in the note) — this is our intended answer to R07.10; (3) any other `every <duration>` → `Interval`; durations `<n><unit>` with units m/min/mins/minute(s), h/hr/hrs/hour(s), d/day(s), bare unit = 1; (4) five or more whitespace fields of `[A-Za-z0-9*,-/]` → `Cron`; (5) ISO timestamp (contains `T` or starts `YYYY-MM-DD`) → `Once`, naive times in the **configured agent timezone** (`timezone` config / `INDUS_TIMEZONE`), not the server's (R07.11); (6) `in <duration>` → `Once` at now + duration computed in UTC; (7) a bare duration (`30m`) → **recurring** `Interval` (R07.9, P6); (8) else a multi-line error listing the accepted forms (our wording). | `cron/jobs.py` [3484: `parse_schedule` at ~783 and the helpers above it], `website/docs/user-guide/features/cron.md` [skim] | `src/indus/cron/grammar.py`, `tests/unit/cron/test_grammar.py` | Table: `30m` and `every 30m` → Interval 30; `in 30m` → Once; `weekdays at 9am` → `0 9 * * 1-5`; `mon, wed and fri 7` → `0 7 * * 1,3,5`; `every 1d at 09:00` → daily 09:00; `2026-11-01T09:00` naive in `Europe/Berlin` config → 08:00Z; `noon`, `midnight`; garbage → error listing forms. Hypothesis: every accepted string round-trips through its display form to the same schedule. |
| P14-02 | **Next-run computation.** Interval: anchor on `last_run_at` (else now) and add minutes in UTC (drift by run duration kept, documented, R07.11). Cron: evaluate on the configured zone's wall clock and convert to UTC; DST fall-back guard tries both folds and picks the earliest instant strictly after the base (a job must not re-fire in the repeated hour); spring-forward gaps skip to the next valid instant. Once: the instant; at create a one-shot more than 120 s in the past is rejected. A recurring job whose next run cannot be computed (bad expression, missing library) goes to recoverable `error` state with the reason, never `completed`, and self-heals when the cause is fixed (R07.12). | `cron/jobs.py` [3484: `compute_next_run` at ~1174, `_oneshot_past_grace_error`] | `src/indus/cron/next_run.py`, `tests/unit/cron/test_next_run.py` | DST: `30 1 * * *` in `America/New_York` on fall-back day fires once (both folds tested); spring-forward `30 2 * * *` fires at 03:00 local once; interval anchored on last run; one-shot 121 s in the past rejected, 119 s accepted; broken expr on a recurring job → `error`, fixed expr → scheduled again. Hypothesis: next run is always strictly after the base. |
| P14-03 | **Job model and store.** Pydantic `CronJob {id: 12 hex (random), name?, prompt?, skills: [], schedule, schedule_display, repeat: {times: int\|null, completed}, enabled, state: scheduled\|paused\|completed\|error, paused_at?, paused_reason?, next_run_at?, last_run_at?, last_status?, last_error?, last_delivery_error?, last_delivery_unverified?, failure_streak, deliver: str, failure_deliver?, origin?: {platform, chat_id, thread_id?}, enabled_toolsets?, workdir?, model?, provider?, base_url?, pinned?, reasoning_effort?, script?, interpreter?, no_agent, monitor_script?\|monitor_url?, monitor_state?, context_from?: [], quota_hold_until?, fire_claim?, created_at}`; authored fields (name, prompt, skills, model/provider/base_url, script, no_agent, monitor, context_from, schedule, deliver, origin, toolsets, workdir, reasoning, failure_deliver) kept apart from scheduler-owned state so imports merge without clobbering run state. Storage `<home>/cron/jobs.json` (dir 0700, file 0600, ownership preserved), atomic replace, guarded by `<home>/cron/jobs.lock` (wait up to 30 s, then a typed busy error). Load repairs a hand-edited file: non-object entries dropped, invalid counts reset, one warning naming only the field types. `enabled` is authoritative; pause markers are a second gate (a half-paused record never fires). Defaults at create: `deliver` = `origin` when created from a chat with an origin, else `local`; one-shot → repeat 1; recurring → forever (≤ 0 = forever); a job needs a prompt, a script or skills. `--paused` creates `enabled=false, state=paused, next_run_at=null` with timestamp and reason ("created paused, waiting for approval" — ours) in one locked write. Job references: id or case-insensitive name; exact id wins; ambiguous names refused with candidate ids. Registers itself as a P09 `SkillReferenceSource` (every job's skill list, paused or not, protects those skills from the curator, R07.8) and implements `rename_skill(old, new)` for curator consolidation. | `cron/jobs.py` [3484: record fields, `create_job` at ~1788, load repair], `cron/job_definition.py` [77], `cron/jobs_invariants.py` [53] | `src/indus/cron/models.py`, `src/indus/cron/store.py`, `tests/unit/cron/test_store.py` | Concurrent writers (two processes) never lose a job; lock wait 30 s then busy error (fake clock); repaired file warns without echoing content; ambiguous name refused with ids; `--paused` record never due; curator protection: P09 curator with this store as source skips a skill used by a paused job; rename rewrites job skills. |
| P14-04 | **Execution ledger.** SQLite `<home>/cron/executions.db` (WAL, P02 writer discipline): attempts `{attempt_id, job_id, scheduled_for (exact instant; null for manual runs), kind: scheduled\|manual\|retry, state: claimed → running → completed\|failed\|unknown, owner_pid, owner_start_fingerprint, heartbeat_at, started_at, finished_at, result_status, delivery_status}`; terminal states immutable. An attempt is recorded **before** dispatch; a (job, scheduled_for) already completed is never replayed even if the jobs file is stale (R07.13). Reaping (every tick and before every manual run): a non-terminal attempt becomes `unknown` only when the owner PID plus process-start fingerprint prove it dead, or a live owner has been silent longer than max(3 × inactivity timeout, script timeout, 2 h); `unknown` attempts are never re-run. History bounded (newest 500 per job); active attempts never pruned. | `cron/executions.py` [653: states, reaping], `cron/occurrences.py` [121: slot identity], `cron/execution_identity.py` [skim] | `src/indus/cron/ledger.py`, `tests/unit/cron/test_ledger.py` | Record-before-dispatch ordering (dispatcher fake asserts the row exists); completed slot not replayed after restoring an old jobs file; dead owner (fake PID table) → unknown; silent live owner past 2 h (fake clock) → unknown; unknown never re-run; terminal update refused. |
| P14-05 | **Store health.** Before any fire, probe that the jobs file and ledger are writable (temp-file create + fsync + unlink in the cron dir); ENOSPC, EROFS, EACCES → **no job fires** (a run that cannot be recorded is not run, R07.13); the ticker keeps running and re-probes about once a minute; `status`, `list` and `doctor` show the condition; emit one notice event when it becomes unwritable and one after an hour writable again; on recovery each still-due job fires once (catch-up rules apply). | `cron/store_health.py` [301: all] | `src/indus/cron/store_health.py`, `tests/unit/cron/test_store_health.py` | Injected ENOSPC → zero dispatches, one notice; recovery → each due job fires once; re-probe interval 60 s (fake clock); recovery notice after 1 h. |
| P14-06 | **Due detection, lateness and catch-up.** Due = enabled, not paused, not in quota hold, `next_run_at ≤ now`. Grace = half the schedule period clamped to [120 s, 7,200 s] (once jobs: 120 s). Lateness classes: dispatched within 300 s of the slot = on time; within grace = late (logged); beyond grace = catch-up: all missed slots collapse into **one** run; a slot already run is never re-run; paused jobs never catch up. `cron.catch_up_missed: false` → a job past grace skips to the following slot without running (skip logged). Resume keeps a slot that came due while paused (one catch-up run, or a logged skip when catch-up is off). Pure functions over (job, now, ledger view). | `cron/jobs.py` [3484: `_compute_grace_seconds` ~937, `_classify_dispatch_lateness` ~952, `_fast_forward_missed_recurring` ~3098] | `src/indus/cron/due.py`, `tests/unit/cron/test_due.py` | Table with fake clock: hourly job 3 h behind → one run then next slot in the future; 5-min job grace = 150 s; daily job grace capped at 7,200 s; catch-up off → skip; paused 2 days then resumed → one run; 299 s late = on time, 301 s = late. |
| P14-07 | **Ticker: `indus cron tick` and `indus cron daemon`.** One pass: acquire the process-wide tick lock `<home>/cron/.tick.lock` (non-blocking; held → exit 0 with "another tick is running"), store-health probe, reap ledger, collect due jobs, claim each with a fire claim on the job record (`{owner, claimed_at, heartbeat_at}`; a claim younger than 300 s is live; heartbeat every 60 s; grace 3×), record the attempt, dispatch into a per-profile pool (`cron.max_parallel_jobs`: unset/0 = unbounded, 1 = serial; `INDUS_CRON_MAX_PARALLEL`); a job already in flight in this process is never re-dispatched; write `ticker_heartbeat` and `ticker_last_success` marker files. `indus cron tick` runs one pass and waits for its jobs; `indus cron daemon` loops every 60 s on an asyncio loop with the injectable clock until SIGINT/SIGTERM (drains in-flight runs up to 30 s). Global emergency pause (`indus cron pause-all [--reason]` / `resume-all`): no scheduled fire starts through any path; in-flight runs continue; manual `run` still executes. P15-21 adds the "gateway owns the ticker" probe to this file. | `cron/scheduler_tick.py` [204: tick flow], `cron/jobs.py` [3484: `claim_job_for_fire` ~2717, `heartbeat_fire_claim`, `_claim_is_live` ~2206], `cron/scheduler.py` [4425: tick entry only] | `src/indus/cron/ticker.py`, `tests/unit/cron/test_ticker.py`, `tests/integration/cron/test_tick_e2e.py` | Two concurrent ticks → second exits; claim younger than 300 s blocks another process; stale claim reclaimed; pool size 1 serialises; pause-all → no scheduled fire, manual run executes; daemon loop with fake clock fires an every-30m job twice in 60 simulated minutes. Integration: real subprocess `indus cron tick` with `INDUS_HOME=<tmp>` runs a script-only job and writes its output document. |
| P14-08 | **Preflight (no tokens on misconfiguration).** Default on (`cron.preflight`). Checks: provider key resolves for the job's route (skipped for unpinned jobs when a fallback chain exists); attached skills resolve through the P09 resolver and their declared env vars, commands and credential files are present; every delivery target is known and has a registered deliverer (`local` and `origin`-resolving-to-local never checked; platform targets before P15 fail with "platform delivery needs the gateway"); each MCP server named in the job's toolsets resolved at least one tool (a server that connected earlier and is merely reconnecting does not block, R07.16); script exists inside `<home>/scripts/`; workdir exists. Failure → `last_status = blocked_config` with the reason, **one** alert per distinct reason (not per tick), **no model call**; the next healthy run clears it. Missing-credential messages name the profile and home path read. | `cron/scheduler_preflight.py` [412: checks, alert-once] | `src/indus/cron/preflight.py`, `tests/unit/cron/test_preflight.py` | Missing key → blocked, fake model never called, one alert across 5 ticks; reconnecting MCP server → passes; platform target before P15 → blocked with the gateway reason; fixed config → cleared on next run. |
| P14-09 | **Script runner and wake gate.** Scripts always live under `<home>/scripts/`; the resolved real path must stay inside (symlink escape refused). `.sh`/`.bash` run under bash; others under our Python or a user-pinned `interpreter`. Environment from the P05 env builder without provider secrets; declared pass-through vars resolved through the job profile's `SecretStore`. Timeout `cron.script_timeout_seconds` default 3,600 (env `INDUS_CRON_SCRIPT_TIMEOUT` → config → default), process-tree kill on timeout. The last stdout line may be a JSON wake gate: `{"wake": false}` → skip this tick silently; `{"wake": true, "context": {...}}` → context passed to the prompt (P14-10). At create, a `script` value that looks like a command line rather than a file name is refused. Used for pre-run scripts and for script-only jobs (H5: trimmed stdout is the message; empty stdout = silent; non-zero exit or timeout = error alert delivered; failure token ignored; no model, no tokens). | `cron/scheduler_script.py` [599: resolution, env, wake gate], `website/docs/guides/cron-script-only.md` [skim] | `src/indus/cron/script.py`, `tests/unit/cron/test_script.py` | Script outside scripts dir refused; symlink escape refused; env lacks provider keys; wake false → silent; wake true context passed; timeout kills tree (fake clock, real short sleep-free child); `script: "ls -la"` refused at create; script-only stdout becomes the message. |
| P14-10 | **Prompt assembly.** Order: our cron hint (written fresh; meaning: this is a scheduled run, the final answer is delivered automatically so do not send it yourself, reply with only the silence token when nothing is new and never mix it with content, put the failure token alone on the first line to record a failure, never create or update cron jobs because the task mentions recurrence; tokens are literal ASCII control words, not to be translated, R07.14) → attached skills loaded exactly as P09 slash invocation does (config blocks included) → `context_from` outputs (each upstream job's most recent completed output ≤ 8,000 chars; upstream framing, or continuity framing for the reserved `self`; silent, no-change and wake-false records skipped when choosing; error documents eligible) → per-job notepad block (P14-17) → wake-gate context and monitor change block (P14-16) → the job prompt → optional `## Run context` block from a manual run (never persisted). The assembled prompt passes the P05 threat scan (scope `cron`); a hit fails the run with a clear reason. The system prompt uses the P04 `cron` surface golden. | `cron/scheduler_prompt.py` [450: order, caps, framing — semantics only] | `src/indus/cron/prompt.py`, `tests/unit/cron/test_prompt.py`, `tests/golden/cron/prompt_assembly.txt` | Golden assembled prompt for a job with two skills, `context_from` of two jobs (one silent → skipped, older non-silent used) and a notepad; 9,000-char upstream clipped to 8,000; `self` continuity framing; injection text in a prompt → run fails before the model; run context not persisted. |
| P14-11 | **Agent job runner.** Fresh session (`source=cron`, id `cron_<job>_<YYYYmmdd_HHMMSS>`, hidden from default lists) through the in-process `SessionService`; memory stores loaded normally; background review skipped (P09-14 flag, R07.5). Toolsets: job `enabled_toolsets` > `cron.toolsets` > the `indus-cron` bundle; an unreadable toolset config fails the run rather than granting all tools. `cronjob_manage` disabled unless `cron.allow_agent_scheduling` (default false, R07.15); interactive tools (`clarify`, messaging) always denied; approvals use `approvals.cron_mode` (default deny, E7). `workdir` (absolute, existing) loads project context files from it and binds tool cwd to the run's task id (safe under parallel runs). Model: per-job pin > `cron.model`/`cron.provider` > main model at fire time; per-job reasoning effort overrides global; credential-pool rotation for all jobs; the fallback chain only for unpinned jobs (a pinned route fails rather than switching model). Inactivity timeout `cron.inactivity_timeout_seconds` 600 (env override); progress stamps keep healthy long runs alive; attempt heartbeat updated with them. Self-removal: a recurring run that removes its own job and then answers still delivers and records `completed`; its output dir is deleted afterwards. | `cron/scheduler.py` [4425: `run_job` at ~2554 — skip worker/systemd plumbing], `website/docs/developer-guide/cron-internals.md` [skim] | `src/indus/cron/runner.py`, `tests/unit/cron/test_runner.py` | Fake model: tool array lacks `cronjob_manage` and `clarify`; dangerous command denied under cron mode deny; pinned route failure does not fall back; unpinned does; workdir context file loaded; inactivity 600 s with no progress → failed (fake clock); review never spawned; self-removing job delivers and records completed. |
| P14-12 | **Result classification and output document.** Failed = runtime exception, timeout, unreachable model, or first line exactly `[JOB_FAILED]` (rest is evidence; elsewhere in the text it is content). Silent = the entire reply, or the opening or closing line alone, equals `[SILENT]` / `SILENT` / `NO_REPLY` / `NO REPLY` (case-insensitive); mid-sentence mentions are content (R07.14, R06.14). Silent → success with delivery suppressed, output still saved. Failed runs always deliver even if the text also looks silent. Output document `<home>/cron/output/<job_id>/<UTC ts>.md` with prompt and response in length-framed sections and, for failures, a redacted chained traceback section; keep newest `cron.output_retention` (50) per job; ≤ 0 disables pruning. Cleanup operations bounded at 10 s each (`cron.cleanup_timeout_seconds`) so a hung finaliser cannot wedge the in-flight guard. | `cron/scheduler.py` [4425: helpers near the silent and failure markers ~527–560, classification near the run end], `cron/delivery_outcome.py` [94] | `src/indus/cron/classify.py`, `src/indus/cron/output.py`, `tests/unit/cron/test_classify.py`, `tests/unit/cron/test_output.py` | Table: `[SILENT]` alone, `nothing new.\n[SILENT]`, `silent` lower-case → silent; "I will stay [SILENT]" → content; `[JOB_FAILED]` on line 1 → failed, on line 2 → content; failed+silent → delivered; 51st document prunes oldest; traceback redacted; hung cleanup abandoned at 10 s (fake clock). |
| P14-13 | **Delivery targets and local delivery.** Target grammar parser shared with P15 (`DeliveryTarget {platform, chat_id?, thread_id?, is_origin, is_explicit, raw}`): `origin`, `local`, `<platform>`, `<platform>:<chat_id>[:<thread_id>]`, `all`, comma lists, deduplicated by (platform, chat, thread); unknown platform names reported as `unknown_platform`, never silently routed to local. `origin` for a job created from inside a cron run is pinned at create to the parent job's concrete target (or `local`) — no dangling origins. Deliverer registry `register_deliverer(platform, fn)`; this phase registers `local` only (the output document plus an optional `cron.local_notify` desktop/terminal notification hook; CLI-created jobs' `origin` resolves to `local`); P15 registers platforms. Delivered text secret-redacted on every lane even with log redaction off; a redactor failure replaces the payload with a fixed notice instead of sending unscanned. Default wrapper: header naming the job and a footer saying the agent cannot see this message (ours; `cron.wrap_response` toggle). Empty payloads never sent. Delivery status kept separate from run status (`delivered \| delivery_failed{reason} \| unverified \| suppressed`); delivery failures never count toward the failure streak (R07.17). | `cron/scheduler_delivery.py` [1923: target tokens, redaction, wrapper, status split — skip platform transports], `cron/scheduler_delivery_origin.py` [23] | `src/indus/cron/targets.py`, `src/indus/cron/delivery.py`, `tests/unit/cron/test_targets.py`, `tests/unit/cron/test_delivery.py` | Parser table incl. `telegram:-100123:42`, `origin,all`, duplicates collapsed, `myspace` → unknown_platform; cron-created job's origin pinned; redactor raising → fixed notice delivered; empty payload not delivered; delivery failure leaves `failure_streak` unchanged. |
| P14-14 | **Bookkeeping, failure streak, unreachable re-runs, quota hold.** After each run: `repeat.completed += 1` (not for retry re-runs), next run computed (P14-02), terminal `completed` when the repeat limit is reached (record kept for inspection). `failure_streak` counts consecutive failed runs (delivery failures excluded, pre-agent crashes included); for recurring jobs reaching `cron.failure_nudge_threshold` (3; 0 disables) appends a review nudge (fix / pause / remove, our wording) to the failure message; success resets; one-shots never nudge. Unreachable model (transient network/DNS error before any model call) on a recurring job → re-run after 5, 15 and 30 minutes, then back to schedule; interim failure notices suppressed; re-runs don't count toward repeat and never pass the next natural occurrence; one-shots excluded; `cron.retry_unreachable: false` disables. Quota hold: when the provider reports a closed usage window with a retry-after and the whole fallback chain is unavailable, park the job until that instant (one alert, missed occurrences coalesced; sparse schedules retry once at the reopening boundary if it is at least half a period early); mid-run 429s are not held; one-shots not held. | `cron/jobs.py` [3484: `_advance_after_run` ~2374], `cron/unreachable_retry.py` [166], `cron/quota_hold.py` [163] | `src/indus/cron/bookkeeping.py`, `tests/unit/cron/test_bookkeeping.py` | Repeat 3 completes after 3 runs; streak 3 adds nudge, success resets; DNS failure → re-runs at +5, +15, +30 min (fake clock) and stops before the next slot; one-shot not retried; quota hold parks until retry-after and coalesces missed slots; mid-run 429 not held. |
| P14-15 | **Incidents.** Group failures by (job, normalised error signature: case, whitespace and measured durations ignored; signature from the first 200 chars; stored error redacted and truncated to 500). States `detected → alerted → resolved \| closed(ack)`. First failure of a signature alerts; repeats are silent while alerted, with one reminder after `cron.failure_repeat_alert_hours` (6; 0 = alert every run); a different error alerts at once; success resolves open incidents (a repeat re-opens); acknowledged incidents stay silent. Incident ledger unreadable → deliver the alert anyway. Stored in `executions.db`. | `cron/incidents.py` [293: signature, states] | `src/indus/cron/incidents.py`, `tests/unit/cron/test_incidents.py` | "timeout after 31.2s" and "Timeout after 45s" share a signature; repeat silent until 6 h reminder (fake clock); different error alerts; success resolves; ack silences; corrupt ledger → alert delivered. |
| P14-16 | **Monitor mode (H7).** Optional `monitor_script` or `monitor_url` (mutually exclusive; incompatible with script-only jobs): run first, hash the exact output bytes; unchanged → run suppressed (`no_change`, saved as such, no model call); changed or first → inject a change block (unified diff ≤ 4,000 chars, new output ≤ 8,000 chars); source failure is an error, never a change, and leaves the stored hash. URL fetch through the P05 SSRF policy, 30 s timeout, ≤ 256 KiB. State `monitor_state {hash, last_output_path}` with the last output in `<home>/cron/output/<job_id>/monitor_last_output.txt`. | `cron/monitor.py` [186: all] | `src/indus/cron/monitor.py`, `tests/unit/cron/test_monitor.py` | MockTransport URL: same body twice → second run suppressed, fake model not called; changed body → diff block ≤ 4,000; 300 KiB body → error; private URL refused; failing script leaves hash unchanged. |
| P14-17 | **Per-job notepad (H17).** Durable key/value store per job in `executions.db` (table ours): values ≤ 16 KB, per job ≤ 64 KB (keys + values); oversize writes raise and leave the store untouched; injected into each run's prompt (P14-10) as a short block; written by `indus cron notepad <job> get\|set\|unset\|list` which the agent calls through `terminal` (no model tool). Deleted with the job. | `cron/notepad.py` [166: limits, injection] | `src/indus/cron/notepad.py`, `tests/unit/cron/test_notepad.py` | 16 KB + 1 value rejected and store unchanged; 64 KB job limit; notepad block appears in the assembled prompt; job removal clears it. |
| P14-18 | **`cronjob_manage` tool (H15).** One tool with `action ∈ {create, list, update, pause, resume, remove, run}`; model-visible fields: job id/name, prompt, schedule, name, repeat, deliver, failure_deliver, skills, script, monitor, no_agent, context_from, continuity, enabled_toolsets, workdir, paused/paused_reason, pinned. `model`, `provider`, `base_url` and `reasoning_effort` are **not** agent-settable; `pinned=true` only locks the current main model and only when the user asked. `update` with `skills=[]` clears skills. `run` is asynchronous: returns a handle at once and the outcome re-enters the conversation through `SessionService` when done; a running job is refused "already running"; surfaces that cannot receive detached results run synchronously. Availability: interactive CLI/TUI, gateway sessions, or a cron session when `allow_agent_scheduling` (R07.15). Create/update reject prompts that match the injection/exfiltration/invisible-Unicode scan and specs that would stop or restart the gateway from inside it (restart-loop guard). A per-job `base_url` (user CLI only) needs an explicit provider and must share the configured endpoint's scheme/host/port so a stored key is never sent elsewhere (R01.11). Schema description and dynamic overrides written fresh. Deferred by default in the tool-search bridge. | `tools/cronjob_tools.py` [1200: schema, availability, run handle], `tools/cronjob_job_args.py` [552: forwarded args], `tools/cronjob_prompt_scan.py` [144] | `src/indus/cron/tool.py`, `tests/unit/cron/test_tool.py` | Agent-supplied `model` ignored/refused; `skills=[]` clears; `run` returns a handle and the result arrives as an internal message; double `run` refused; tool absent in a cron session by default and present with opt-in; injection prompt refused; `systemctl restart indus-gateway` job refused. |
| P14-19 | **`indus cron` CLI, `/cron`, status and doctor (H16).** Verbs: `create <schedule> <prompt> [--name] [--skill …] [--deliver] [--script] [--no-agent] [--workdir] [--toolsets] [--model --provider] [--paused [--reason]]`, `edit <job> [--skill replaces list] [--add-skill] [--remove-skill] [--clear-skills] [--workdir ""]`, `list` (overdue labelling when next run is > 15 min in the past, streaks, delivery warnings), `pause`, `resume`, `run <job> [--context …]`, `remove`, `status` (ticker alive from the heartbeat marker, last success, soonest run ordered by instant, store health, global pause), `tick`, `daemon`, `pause-all`/`resume-all`, `runs <job>`/`history`, `incidents [--ack <id>]`, `notepad`, `doctor` (read-only per active job: last run failed, delivery failed or unverified, late or catch-up dispatch, missing or > 15-min-overdue next run, missing or escaping script, script-only without script, vanished workdir; exit 1 while any finding stands). Slash `/cron add\|list\|edit\|pause\|resume\|run\|remove` on CLI, TUI and (P15) gateway. Non-TTY rules per P07. | `hermes_cli/cron.py` [1005: verbs], `cron/scheduler_diagnostics.py` [skim] | `src/indus/cli/commands/cron.py`, `src/indus/cron/doctor.py`, `tests/unit/cli/commands/test_cron.py`, `tests/unit/cron/test_doctor.py` | CliRunner per verb on a fixture home; `--skill a --skill b` replaces, `--add-skill` appends; overdue label at 16 min; `doctor` exits 1 with a vanished workdir and 0 when healthy; `status` reads markers; `/cron add "every 30m" check the build` creates a recurring job with `deliver=local`. |

## Files to create (47)
| Path (under `indus-agent/`) | What it holds | Task |
|---|---|---|
| `src/indus/cron/grammar.py` | schedule grammar | P14-01 |
| `tests/unit/cron/test_grammar.py` | grammar table + properties | P14-01 |
| `src/indus/cron/next_run.py` | next-run, DST guard | P14-02 |
| `tests/unit/cron/test_next_run.py` | DST and anchors | P14-02 |
| `src/indus/cron/models.py` | job model | P14-03 |
| `src/indus/cron/store.py` | JSON store, lock, repair, skill reference source | P14-03 |
| `tests/unit/cron/test_store.py` | store cases | P14-03 |
| `src/indus/cron/ledger.py` | execution ledger | P14-04 |
| `tests/unit/cron/test_ledger.py` | ledger cases | P14-04 |
| `src/indus/cron/store_health.py` | writability gate | P14-05 |
| `tests/unit/cron/test_store_health.py` | health cases | P14-05 |
| `src/indus/cron/due.py` | due, lateness, catch-up | P14-06 |
| `tests/unit/cron/test_due.py` | lateness table | P14-06 |
| `src/indus/cron/ticker.py` | tick pass, daemon loop, claims, pool, global pause | P14-07 |
| `tests/unit/cron/test_ticker.py` | ticker cases | P14-07 |
| `tests/integration/cron/test_tick_e2e.py` | real `indus cron tick` subprocess | P14-07 |
| `src/indus/cron/preflight.py` | preflight checks | P14-08 |
| `tests/unit/cron/test_preflight.py` | preflight cases | P14-08 |
| `src/indus/cron/script.py` | script runner, wake gate | P14-09 |
| `tests/unit/cron/test_script.py` | script cases | P14-09 |
| `src/indus/cron/prompt.py` | prompt assembly + our cron hint | P14-10 |
| `tests/unit/cron/test_prompt.py` | assembly cases | P14-10 |
| `tests/golden/cron/prompt_assembly.txt` | golden assembled prompt | P14-10 |
| `src/indus/cron/runner.py` | agent job runner | P14-11 |
| `tests/unit/cron/test_runner.py` | runner cases | P14-11 |
| `src/indus/cron/classify.py` | silence/failure classification | P14-12 |
| `src/indus/cron/output.py` | output documents + retention | P14-12 |
| `tests/unit/cron/test_classify.py` | token table | P14-12 |
| `tests/unit/cron/test_output.py` | document cases | P14-12 |
| `src/indus/cron/targets.py` | target grammar (shared with P15) | P14-13 |
| `src/indus/cron/delivery.py` | deliverer registry, local delivery, redaction, status | P14-13 |
| `tests/unit/cron/test_targets.py` | parser table | P14-13 |
| `tests/unit/cron/test_delivery.py` | delivery cases | P14-13 |
| `src/indus/cron/bookkeeping.py` | repeat, streak, unreachable ladder, quota hold | P14-14 |
| `tests/unit/cron/test_bookkeeping.py` | bookkeeping cases | P14-14 |
| `src/indus/cron/incidents.py` | incidents | P14-15 |
| `tests/unit/cron/test_incidents.py` | incident cases | P14-15 |
| `src/indus/cron/monitor.py` | monitor mode | P14-16 |
| `tests/unit/cron/test_monitor.py` | monitor cases | P14-16 |
| `src/indus/cron/notepad.py` | notepad | P14-17 |
| `tests/unit/cron/test_notepad.py` | notepad cases | P14-17 |
| `src/indus/cron/tool.py` | `cronjob_manage` | P14-18 |
| `tests/unit/cron/test_tool.py` | tool cases | P14-18 |
| `src/indus/cli/commands/cron.py`, `src/indus/cron/doctor.py` | CLI and doctor (2 files) | P14-19 |
| `tests/unit/cli/commands/test_cron.py`, `tests/unit/cron/test_doctor.py` | CLI and doctor tests (2 files) | P14-19 |


## Files to update (9)
| Path (under `indus-agent/`) | Change | Task |
|---|---|---|
| `src/indus/learning/curator/__init__.py` (P09) | none to the protocol; the cron store registers itself at service start (`service/` wiring below) | P14-03 |
| `src/indus/service/host_inprocess.py` (P04) | register the cron store as a `SkillReferenceSource`; expose detached-result injection used by `cronjob_manage run` | P14-03, P14-18 |
| `src/indus/tools/toolsets.py` (P04) | `cronjob` toolset, `indus-cron` bundle (no `clarify`, no `cronjob_manage` unless allowed) | P14-11, P14-18 |
| `src/indus/safety/threat_scan.py` (P04/P09) | `cron` scope | P14-10, P14-18 |
| `src/indus/cli/app.py` (P06) | lazy group `cron` | P14-19 |
| `src/indus/service/commands/registry.py` (P06-08) | `/cron` | P14-19 |
| `src/indus/foundation/config/schema.py` (P01) | `cron.*` keys with the defaults above; `timezone` if not already present | each owning task |
| `docs/plan/phases/00-layout-and-conventions.md` §4 | pin row for `croniter` | P14-01 |
| `src/indus/cron/__init__.py` (P00-10 placeholder) | package exports | P14-01 |

## Fixtures and fakes (4)
| Path | What it holds | Task |
|---|---|---|
| `tests/fixtures/cron/jobs_*.json` | valid, hand-edited/corrupt and legacy-shaped job files | P14-03 |
| `tests/fixtures/cron/scripts/` | wake-gate scripts, slow script, failing script, symlink escape | P14-09 |
| `tests/fixtures/model_scripts/cron_*.yaml` | fake-model runs: silent, failure token, tool use, unreachable before call | P14-11, P14-12, P14-14 |
| `tests/fakes/pids.py` | fake PID/start-time table for ledger reaping | P14-04 |

## Task notes (19)
`docs/notes/P14/<slug>.md`: `cron-grammar` (01), `cron-next-run` (02), `cron-store` (03),
`cron-ledger` (04), `cron-store-health` (05), `cron-due` (06), `cron-ticker` (07),
`cron-preflight` (08), `cron-script` (09), `cron-prompt` (10), `cron-runner` (11),
`cron-classify-output` (12), `cron-delivery` (13), `cron-bookkeeping` (14), `cron-incidents` (15),
`cron-monitor` (16), `cron-notepad` (17), `cron-tool` (18), `cron-cli` (19).

## Run until green
```bash
cd indus-agent
uv sync --locked
uv run ruff check . && uv run ruff format --check .
uv run pyright && uv run lint-imports
uv run pytest -q tests/unit/cron tests/unit/cli/commands/test_cron.py tests/integration/cron
uv run pytest -q tests/golden            # cron surface prompt + assembled prompt goldens
uv run pytest -q
uv run python -m tooling.checks --require-ref
```
On any failure, fix the cause and rerun from `uv sync`. Never skip a DST or at-most-once test,
widen a grace window to pass a test, or hand-edit a golden prompt.

## Compare checklist (reopen the reference after writing)
- [ ] Grammar order and every accepted form; bare duration recurring; `in` once; naive ISO in the configured zone (R07.9, R07.11); `every Nd at HH:MM` behaviour recorded as D-16.54 (R07.10).
- [ ] Next run: interval anchored on last run in UTC; cron on wall clock with both-folds guard; 120 s one-shot grace; recurring jobs never silently complete (R07.12).
- [ ] Store lock 30 s, atomic, permissions, repair on load, authored vs scheduler fields.
- [ ] At-most-once: attempt before dispatch, never fire when unwritable, unknown never re-run, reaping rule max(3×, script, 2 h) (R07.13).
- [ ] Lateness 300 s / grace half-period [120, 7,200] / catch-up collapse / paused never catch up.
- [ ] Claims 300 s, heartbeat 60 s, grace 3×; tick lock; pool sizes; global pause semantics.
- [ ] Preflight alerts once, no LLM call, reconnecting MCP passes (R07.16).
- [ ] Script dir confinement, 3,600 s, wake gate; script-only semantics.
- [ ] Prompt order and caps (8,000; notepad; run context not persisted); cron tools off unless allowed (R07.15).
- [ ] Silence and failure token rules; failed runs always deliver (R07.14, R06.14).
- [ ] Delivery status separate; redaction fail-closed; empty never sent (R07.17).
- [ ] Streak 3, unreachable 5/15/30 min, quota hold, incidents 200/500/6 h.
- [ ] Intended differences (our tokens `[JOB_FAILED]`, `wake`; ticker outside the gateway until P15; local-only delivery) have D-ids.

## Traps covered
| Trap | Pinned by test in |
|---|---|
| R07.8 cron-referenced skills protected; renames rewrite jobs | P14-03 (with P09-18/19) |
| R07.9 bare duration recurring | P14-01 |
| R07.10 "every 1d at 09:00" decided (D-16.54) | P14-01 |
| R07.11 timezones and DST | P14-01, P14-02 |
| R07.12 recurring jobs never silently complete | P14-02 |
| R07.13 at-most-once, unwritable store, unknown attempts | P14-04, P14-05, P14-07 |
| R07.14 silence/failure tokens | P14-12 |
| R07.15 cron recursion | P14-11, P14-18 |
| R07.16 preflight once, no spend | P14-08 |
| R07.17 delivery ≠ run success | P14-13, P14-14 |
| R07.5 no review for cron runs | P14-11 |
| R06.14 silence never hides failures | P14-12 |
| R01.11 key scoped to its host (`base_url` rule) | P14-18 |
| R02.22 secrets in child processes (scripts) | P14-09 |

## Acceptance
| Feature | Planned ids |
|---|---|
| H1 job store + output documents | ACC-H1-1 |
| H2 schedule grammar | ACC-H2-1 (forms), ACC-H2-2 (DST fall-back) |
| H3 ticker, lateness, catch-up, claims | ACC-H3-1 (catch-up once), ACC-H3-2 (claims); gateway hosting reviewed in P15-21 |
| H4 agent jobs | ACC-H4-1 (fresh session, skills, context_from, workdir, pins) |
| H5 script-only jobs | ACC-H5-1 |
| H6 pre-run script wake gate | ACC-H6-1 |
| H7 monitor mode | ACC-H7-1 |
| H8 preflight | ACC-H8-1 |
| H9 silence and failure tokens | ACC-H9-1 |
| H10 delivery (local half) | ACC-H10-1 (local); platform half reviewed in P15-20 |
| H12 streak, unreachable re-runs, quota hold, incidents | ACC-H12-1, ACC-H12-2 |
| H13 execution ledger | ACC-H13-1 (unknown never re-run) |
| H15 `cronjob_manage` | ACC-H15-1 (actions), ACC-H15-2 (recursion guard) |
| H16 `indus cron` and `/cron` | ACC-H16-1 |
| H17 notepad | ACC-H17-1 |
| E7 unattended cron approvals deny | ACC-E7-1 |

## Done when
- All suites green including DST and at-most-once tests; goldens unchanged; `tooling.checks
  --require-ref` clean.
- Demo (M4 second half): `/cron add "every 30m" check the build` creates a recurring job; with the
  fake model and a fake clock in CI (real key and `indus cron daemon` locally) it fires every 30
  minutes, writes `<home>/cron/output/<id>/<ts>.md`, stays silent when the reply is `[SILENT]`,
  delivers a failure when the first line is `[JOB_FAILED]`; killing the process mid-run leaves the
  attempt `unknown` after reaping and it is never re-run; a job with a missing key is blocked once
  with no model call.
</content>
</invoke>
