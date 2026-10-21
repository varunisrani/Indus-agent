# Task: Memory caps for background processes (P24-43) Phase: 24 Owner: wave-38 builder S1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/process_registry.py` (the
systemd scope branch near the top: probe, TTL, argv shape, cap bounds; a grep of the rest of that
branch for the unit name and bus derivation). Closed before writing.
Research: `docs/plan/research/02-tools.md` §3.12 (spawn local, teardown order). Evidence: none.

What it does (own words): on Linux with a reachable systemd user manager, every local background
process (`terminal` with `background: true`, P05-13) is started inside its own transient scope:
`systemd-run --user --scope --quiet --collect --unit indus-bg-<id> --property MemoryMax=<bytes>
[--expand-environment=no] -- bash -c …`. The scope gives the job its own cgroup with a memory cap,
so a runaway build is killed alone and — when the gateway itself runs under a unit with
`MemoryMax` — it is moved out of the gateway's cgroup, so the OOM killer cannot take the whole
gateway down. `systemd-run --scope` execs the command in the same process, so pid, process group,
output pipe and our ordered kill are unchanged.

Cap: `terminal.background_memory_max` — a size (`1G`, `512M`, `768MiB`, a bare number = MiB) or
`off`/`0`/`false` to disable; default 1 GiB, clamped to 64 MiB … 4 GiB (one warning per odd value).

Availability: Linux only (never Windows, never macOS); `systemd-run` on the job's `PATH`; a user
bus (`DBUS_SESSION_BUS_ADDRESS`, else `$XDG_RUNTIME_DIR/bus`, else `/run/user/<uid>/bus` owned by
us — the derived address is added to the child env so `systemd-run` reaches it); then a probe run
(`… -- /bin/true`, 5 s) whose verdict is cached for 60 s (both answers expire: a bus can come and
go). Older systemd rejects `--expand-environment=no`; the probe then retries without it and the
choice is remembered. Unavailable → the job starts unscoped and one debug line says why (once per
reason).

OOM: a scoped job whose leader died of SIGKILL that Indus did not send is reported as
`reason: killed`, `source: memory cap` ("was stopped (memory cap)" in the completion notice;
`completion_reason: killed`, `termination_source: memory cap` in `process_manage`).

Inputs → outputs: `SpawnRequest` (scope, env) → wrapped argv + `record.memory_cap`; exit code →
reason/source.

Files: `src/indus/backends/memory_scope.py`, `tests/unit/backends/test_memory_scope.py`,
`tests/fakes/systemd_run.py`; shared: `src/indus/backends/processes.py` (wrap + OOM mapping),
`foundation/config/schema.py` (`terminal.background_memory_max`).

Edge cases / failure modes:
- probe hangs → 5 s timeout counts as unavailable; probe binary vanished later → spawn fails into
  the normal `failed_start` path (the registry already handles a failed launch).
- PTY jobs are wrapped the same way.
- a job killed by `process_manage kill` keeps `killed: process_manage kill` (our kill wins).
- remote backends (Docker, SSH …) are never wrapped: the cap is a host cgroup feature.

Our design differences / decisions:
- one knob in config instead of an env override; out-of-range values are clamped, not ignored.
- no "restart-safe scope" policy for gateway children (that is cron/gateway supervision, not this
  row); no reading of the gateway's own cgroup limit to tighten the cap.
- OOM detection by "SIGKILL not sent by us" on a scoped job (the scope is collected at exit, so
  its `memory.events` is usually gone).

Tests we will write:
- argv carries `MemoryMax=<bytes>` and the unit; clamping (too small, too big, units, off);
  no user bus → no scope + exactly one log line; non-Linux → never; probe cached and expiring;
  `--expand-environment=no` rejected → dropped; real registry spawn through the fake
  `systemd-run` (records argv, execs the command); a self-SIGKILL job → `killed: memory cap`;
  our own kill keeps its source.

## Decisions for the lead
- `terminal.background_memory_max` accepts sizes or `off`; default `1G`.

Compare checklist (filled after the rewrite; reopened `tools/process_registry.py` top branch):
- [ ] cap bounds and default
- [ ] probe TTL both ways; option fallback for old systemd
- [ ] Linux-only gate
- Deliberate differences: see above.
- Not taken: see above.

## Status

Done except the Compare step (2026-10-21): memory_scope.py, processes.py wiring, schema row + test, fake systemd-run, tests. Compare checklist NOT yet walked.
