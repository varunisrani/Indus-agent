# Task: Docker orphan reaper (P11-07) Phase: 11 Owner: P11 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/docker.py`
(`reap_orphan_containers`, `_container_finished_at`), research `02-tools.md` §3.8, traps R02.8,
R02.23. Closed before writing.
Evidence: none.

What it does (own words): persistent containers are left running for reuse, and a crash can leave
any container behind, so once per process (the first time a Docker environment is opened, when
`terminal.docker_orphan_reaper` is on) Indus sweeps its own leftovers for this profile: stopped
containers that finished more than 24 h ago are removed, and running session containers whose
session is gone and whose task id has no live background job are removed. Containers without the
`indus-agent=1` label, or of another profile, are never touched. The sweep runs as a detached task
with a 30 s budget; when the budget runs out it stops and logs what it skipped.

Inputs → outputs: `reap_orphans(cli, *, profile_label, clock, keep, session_alive, jobs_alive,
budget_s=30, stopped_after_s=86400)` → `ReapReport(removed, kept, budget_exceeded)`;
`start_reaper_once(...)` schedules it once per (client, profile).

Files: `src/indus/backends/docker/reaper.py`, `tests/unit/backends/docker/test_reaper.py`.

Edge cases / failure modes:
- `ps`/`inspect` failures → skip that container (or the whole sweep), never raise;
- `FinishedAt` zero value or unparseable → keep (unknown age);
- labels re-checked through `inspect` before any removal;
- a stopped container is removed with plain `rm` (the daemon refuses a running one, which is the
  atomic re-check against a sibling restarting it); a running orphan needs `rm -f`;
- the container this process just attached to is in `keep`;
- the profile-shared container (`indus-task-id=shared`) is never removed while running.

Our design differences / decisions:
- Reference: exited containers only, older than 10 min. Ours (spec): stopped > 24 h, plus running
  session containers whose session and jobs are gone. "Session gone" is a predicate the host
  supplies (session store); the default treats every session as alive, so running containers are
  only reaped where the host can tell. Proposed D-id.

Tests we will write: fake CLI with mixed containers: only stale labelled ones removed; unlabelled
untouched; another profile's untouched; running orphan removed only when session and jobs are gone;
zero FinishedAt kept; budget exceeded → stops and logs; once per process.

## Decisions for the lead
- Wire `session_alive` from the session store (ended or missing row → gone) where the host opens
  Docker environments; until then running containers are never reaped (safe side).

Compare checklist (filled after the rewrite; reopened `tools/environments/docker.py`):
- [x] profile filter and label filter (test: `tests/unit/backends/docker/test_reaper.py::test_only_stale_labelled_containers_of_this_profile`)
- [x] plain `rm` for stopped, unknown age kept (test: `::test_zero_finished_at_is_kept`)
- [x] budget (test: `::test_budget_exceeded_stops_and_logs`)
- Deliberate differences: 24 h instead of 10 min; running orphans considered (spec).
- Not taken: nothing else.

## Status

Done (2026-10-18).

## Integration (lead)

Merged in the P11 integration (2026-10-18, lead's verify tree on main with P08 wave 1; slices applied A, B, C).
- Liveness wired by the host (`service.terminal_host`): `session_alive` asks the run's session store (fail-safe toward alive: a missing row counts as alive because the answers are process-wide while one process may serve several profiles); `jobs_alive` asks the process registry (a running job whose session id or env key names the task). D-16.124.
