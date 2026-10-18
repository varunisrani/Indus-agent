# Task: Docker recovery and isolation flag (P11-06) Phase: 11 Owner: P11 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/docker.py`
(`_NO_CONTAINER_PATTERNS`, `_is_container_gone`, `_recreate_container`, `execute`), research
`02-tools.md` §3.9 ("Recreated flag"), §3.13 (order step 1), traps R02.4, R02.6, R02.19.
Closed before writing.
Evidence: none.

What it does (own words): a container can vanish under a running session (someone ran
`docker rm`, a prune, an OOM kill, a daemon restart). When a command's output says the container is
gone ("No such container", "is not running"), the environment looks for a reusable labelled
container (another process may already have made one) or creates a fresh one, takes a new
snapshot, runs the command once more and marks that result `environment_recreated`, so the tool
warns that background processes and unsaved files may be gone (R02.19). This happens only with
`docker_persist_across_processes`; without it the failure becomes a connection error, the registry
answers "degraded" and the next call starts clean. A second "gone" in a row is returned as is.
Background jobs the old container ran are ended as `lost` right there, through the lost
handlers (`register_lost_handler`; `mark_lost(registry, env_key)` does the marking), not left to
the one-shot `environment_recreated` flag that a remote poll may consume; the environment also
counts `recreations`, which pollers can compare at any time.

Isolation: the environment's `BackendFlags.isolated` is true only when no host path is bound
(`MountPlan.host_paths_mounted`), so the approval pipeline skips prompts only then — deny globs and
the hardline floor still apply (R02.6, R02.4).

Inputs → outputs: `container_gone(output)`, `daemon_gone(output)` (pure); `recover(env)` → bool;
`register_lost_handler(fn)` / `notify_lost(env_key)`; `mark_lost(registry, env_key)` → ids marked;
`isolated_flags(host_paths_mounted)` → `BackendFlags`.

Files: `src/indus/backends/docker/recovery.py`, `tests/unit/backends/docker/test_recovery.py`.

Edge cases / failure modes: the pattern must open the output (the client prints its error before
anything else), so a user command that merely prints "No such container" is not mistaken for it;
recovery failure (create fails) → the original result is returned; a lost-handler that raises is
logged and does not stop recovery.

Our design differences / decisions:
- Without persist-across-processes we degrade and evict instead of returning the raw error over
  and over (the reference keeps the broken environment until idle reap).
- Pattern match is anchored to the start of the output.
- The P05 sudo rewrite is never applied on Docker (`env.sudo` stays None): a host password must
  not reach a container unless a container-specific setting asks for it (none exists yet).

Tests we will write: container removed between calls → one recreate + retry +
`environment_recreated`; second failure surfaces the error; persist off → degraded; background job
marked lost; mounted cwd → `isolated=False` and an `rm -rf /workspace` goes to the approval flow;
unmounted → prompt skipped but a deny glob still blocks.

Compare checklist (filled after the rewrite; reopened `tools/environments/docker.py`):
- [x] patterns and retry once (test: `tests/unit/backends/docker/test_recovery.py::test_removed_container_is_recreated_once`)
- [x] reuse a labelled container first (test: `::test_recovery_prefers_a_labelled_container`)
- [x] snapshot re-run in the new container (test: `::test_removed_container_is_recreated_once`)
- [x] lost jobs marked at recovery, counter kept (tests: `::test_background_job_marked_lost`, `::test_removed_container_is_recreated_once`)
- [x] isolation by mounts (tests: `::test_mounted_cwd_goes_through_approval`, `::test_unmounted_skips_prompt_but_deny_glob_blocks`)
- Deliberate differences: degrade instead of raw repeats without persist; anchored patterns.
- Not taken: nothing else.

## Status

Done (2026-10-18). The lost-job hook is wired by the host or slice B (see docker-environment note).

## Integration (lead)

Merged in the P11 integration (2026-10-18, lead's verify tree on main with P08 wave 1; slices applied A, B, C).
- Lost jobs: recovery calls `notify_lost(env_key)` and the host handler (see docker-environment) ends that environment's jobs as `lost` right away. Remote pollers also compare `recreations` (`remote_processes.generation`) and never signal a job whose sandbox was replaced (its pid names something else now).
- The one-shot `environment_recreated` flag is only peeked at by a poll (put back with `mark_recreated`), so the next foreground command still carries the warning.
