# Task: Curator phase 1, deterministic lifecycle (P09-18) Phase: 09 Owner: builder slice F

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/curator.py` (defaults, state
file, `should_run_now`, `apply_automatic_transitions`, the run claim, report writing),
`tools/skill_usage.py` (eligibility, archive / restore, curated report — skimmed for semantics),
`website/docs/user-guide/features/curator.md` (skim; its numbers are stale, R07.7/R07.30).
Evidence: none.

What it does (own words): an idle-time janitor for skills the background review wrote. Only skills
whose usage record carries `origin: agent` (and that are neither bundled nor hub-installed) are in
its care; everything a person wrote, `/learn` made, or that lives in an external or project folder
is never touched. It ages its skills by their last activity: unused for 14 days they become
`stale`, unused for 30 days their folder is moved into `<home>/skills/.archive/<name>/` (never
deleted). A pinned skill, or one that a registered reference source (cron jobs, from P14) names, is
skipped. It runs at most once per interval (168 h), only when the agent has been idle for 2 h, and
on a fresh install the first look only records "now" so nothing moves until one interval has
passed. Each run writes `run.json` and `REPORT.md` under `<home>/logs/curator/<stamp>/`.

Inputs → outputs:
- config `curator.{enabled, interval_hours, min_idle_hours, stale_after_days, archive_after_days,
  consolidate, prune_builtins, archive_ttl_days, backup.{enabled, keep}}` (code values 168 / 2 / 14 /
  30, D-16.24), the usage sidecar (P09-12), `.bundled_manifest`, `.hub/lock.json`, the reference
  registry → state file `<home>/skills/.curator_state` `{last_run_at, paused, claim, run_count,
  last_summary, last_report}`, usage `state`/`archived_at` changes, archive moves, reports.
- `should_run(now, idle_s)`, `maybe_run(scope, idle_s=…, gateway_running=…)`, `run_curator(scope,
  dry_run=…, consolidate=…)`, `apply_transitions(scope, now)`, `MaintenanceTimer` (service).

Files: `src/indus/learning/curator/{__init__,settings,state,archive,lifecycle,report,ledger_port}.py`,
`src/indus/service/maintenance.py`, `tests/unit/learning/curator/{test_lifecycle,test_archive}.py`,
`tests/unit/learning/curator/{_helpers_f,_stubs_f}.py` (the stubs file is the STAND-IN for slice
D's ledger and `skill_manage`), `tests/unit/service/test_maintenance.py`, `tests/fakes/skill_refs.py`;
shared: `foundation/config/schema.py` (+ its test), `docs/internals/{traps-ledger,acceptance-map}.md`
(R07.6-R07.8, ACC-F21-1 rows).

Edge cases / failure modes:
- no state file → seed `last_run_at = now`, return "not due" (R07.7); unreadable state file → treat
  as empty, and seed (never run on a damaged file's say-so).
- a record with neither `last_activity_at` nor `created_at` → its `created_at` is seeded to now and
  it is left alone this run (never aged from the epoch).
- never-used skills younger than `stale_after_days` stay active (a stale one is reactivated).
- an activity newer than the stale cut-off brings a stale skill back to active.
- a reference source that raises → the protected set is unknown, so nothing is archived this run
  (stale marking still happens); logged. Safer than treating the failure as "nothing referenced".
- config values < 1 for the day/hour counts fall back to the defaults with one warning (an
  unconfirmed automatic pass must never archive everything because of a typo).
- archive target name taken → a `-<utc stamp>` suffix; a skill under `skills.create_dir` is
  archived into the home archive too.
- two processes: the run claim is written into the state file under its lock; a claim older than
  3,600 s is stale and taken over.
- dry run: no transitions, no `last_run_at` bump; the report says what would move.

Our design differences / decisions:
- module split: settings / state / archive / lifecycle / report instead of one big file.
- a failing reference source blocks archiving for that run (reference: treated as empty).
- the claim lives inside `.curator_state` (the plan's shape) instead of a separate lock file.
- bundled skills (only with `prune_builtins`) age from the first time the curator saw them,
  because P09-12 does not record telemetry for bundled skills; archive-only, never consolidated.
- ledger writes for archive/restore/purge go through a small `LedgerPort` (slice D's ledger plugs
  in at integration); with no port, nothing is ledgered and nothing fails.
- a profile with a running gateway leaves the check to the gateway: `maybe_run` takes a
  `gateway_running` predicate (P15 supplies it).

Tests we will write: fresh install seeds and does not run; 169 h later with 3 h idle runs, 1 h idle
skips; agent skill 15 d → stale, 31 d → archived (folder moved, usage `archived`); learn-origin and
hand-written skills untouched at 400 d; pinned skipped; reference source protects; failing source
blocks archive; never-used 10-day-old skill stays; stale claim (3,601 s) reclaimed, fresh claim
blocks; report written with "(not resolved)" and 0 s when no candidates; paused → not due;
maintenance timer first poll at 90 s then hourly (fake clock).

## Wiring for the lead
- CLI session start: `indus.learning.curator.lifecycle.maybe_run(scope, idle_s=<seconds since the
  profile's last message>, gateway_running=<P15 predicate>)` in a background thread (it never
  raises). `cli/runtime.py` is not edited here.
- Service: `indus.service.maintenance.MaintenanceTimer(clock)`; register
  `curator_check(scope, idle_s=…)` (exported by `lifecycle`) and start `timer.run()` as a service
  task; `timer.stop()` on shutdown.
- P14 registers the cron store with `indus.learning.curator.register_reference_source(...)`.
- Slice D: `skill_manage` deletes from the review fork should call
  `indus.learning.curator.archive.archive_skill(scope, name, actor=…)`; the ledger is plugged in with
  `indus.learning.curator.ledger_port.set_ledger(port)` (protocol in that module).

## Decisions for the lead
- `curator.prune_builtins` ages bundled skills from first sight (no bundled telemetry); it stays off.
- A raising `SkillReferenceSource` stops archiving for that run (fail safe).
- Config keys added: the `curator` section and `auxiliary.curator` (shared schema file).

Compare checklist (filled after the rewrite; reopened `agent/curator.py`, `tools/skill_usage.py`):
- [x] defaults 168 h / 2 h / 14 d / 30 d; values < 1 fall back with one warning
  (test: `test_lifecycle.py::test_bad_numbers_fall_back`)
- [x] first observation seeds and defers (test: `test_lifecycle.py::test_fresh_install_seeds_only`)
- [x] pinned and referenced skills skipped (tests: `test_pinned_skipped`, `test_reference_source_protects`)
- [x] never-used skill not archived before `stale_after_days` (test: `test_never_used_young_skill_kept`)
- [x] stale → active when used again (test: `test_reactivated_after_use`)
- [x] claim stale after 3,600 s (test: `test_live_claim_blocks_stale_claim_reclaimed`)
- [x] archive is a move, never a delete; name collision gets a stamp suffix (test:
  `test_archive.py::test_archive_collision_gets_suffix`)
- [x] restore refuses a hub/bundled owner and an existing destination (tests in `test_archive.py`)
- [x] report written even when nothing ran (test: `test_report_without_candidates`)
- [x] gap found: the reference ages old snapshots out on a prune-only run too — fixed in
  `run_curator`, pinned by `test_backup.py::test_prune_only_run_ages_snapshots`
- Deliberate differences: reference treats a failing cron read as "nothing referenced"; we stop
  archiving for that run. Reference keeps the run claim in a separate lock file; ours is in the
  state file (the plan's shape). Reference records telemetry for bundled skills; ours does not
  (P09-12), so prune_builtins ages them from first sight.
- Not taken: the per-run metric record (no metrics sink yet); protected built-in name list (our
  starter set has no slash-command entry points that depend on a skill).

## Status

Done (2026-10-19). Shared: `foundation/config/schema.py` + test (curator section,
`auxiliary.curator`). Host wiring is the lead's (above).

## Integration (lead, P09 wave 2 landing)
Hosts: `cli/runtime.learning_startup` runs `maybe_run` on a daemon thread at interactive chat start (idle = time since the newest stored message, `service.maintenance.profile_idle_s`); an interactive service carries `learning_timer` (curator check, consolidation on the service's auxiliary client), started with the first session and stopped at `aclose`. `ledger_port` defaults to the profile's `SkillLedger`. `_stubs_f.py` deleted; tests use the real ledger and `skill_manage`. D-16.156.
