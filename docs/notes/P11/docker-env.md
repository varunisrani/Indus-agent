# Task: Docker environment variables by name only (P11-04) Phase: 11 Owner: P11 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/docker.py`
(`_normalize_forward_env_names`, `_normalize_env_dict`, `_name_only_env_args`, the env half of
`_egress_and_env_args`, `_build_init_env_args`, `_build_runtime_env_args_with_unsets`,
`_run_bash`), `tools/environments/remote_common.py` (`resolve_passthrough_env`, `prepend_unset`,
`client_env_with`), research `02-tools.md` §3.10/§3.14, traps R02.7, R02.22. Closed before writing.
Evidence: none.

What it does (own words): variables the user wants inside the container travel as `-e NAME` with
no value; the docker client reads each value from its own process environment. A value on the
command line would be readable by every local user through `/proc/<pid>/cmdline` (R02.7); the
client's environment is owner-only. The names come from three places: `terminal.docker_forward_env`
(values from the profile's secret store, else its env), `terminal.docker_env` (explicit values in
config) and the passthrough names of P05 (`terminal.env_passthrough` and skill-declared names).
Names Indus manages (provider keys, `INDUS_*`, …) never pass, whatever the config says (R02.22).
Forwarded and passthrough values are injected again on every `exec`, so an updated secret is seen at
once and one container serving several sessions of a profile always gets the current values; a
name that has no value now is unset in the command so a value baked into the container at create
time cannot leak.

Inputs → outputs: `plan_env(scope, settings)` → `EnvPlan(run_names, exec_names, values, unset,
refused)`; `name_flags(names)` → `["-e", N, ...]`; `client_env(scope, values)` → the docker client's
environment: the minimal child env (P05 builder, `minimal` variant) plus the client's own control
variables (`DOCKER_*`, `CONTAINER_HOST`, `XDG_RUNTIME_DIR`, …) plus the values; `redact_values(text,
values)` for error text.

Files: `src/indus/backends/docker/env.py`, `tests/unit/backends/docker/test_env.py`.

Edge cases / failure modes: invalid names (not `[A-Za-z_][A-Za-z0-9_]*`) dropped with a warning;
non-scalar `docker_env` values dropped; duplicates kept once; a blocked name in any source is
refused and logged once (the value never read); an empty value is still a value.

Our design differences / decisions:
- The reference lets `docker_forward_env` bypass the credential blocklist (operator opt-in); we
  never re-allow a blocked name (spec, R02.22). Proposed D-id.
- The forwarded/passthrough names are kept out of the shell snapshot (P05's `excluded_names`) so
  a stale value is never re-sourced.

Tests we will write: fake CLI records argv and env: `-e OPENAI_API_KEY` never present (blocked
even when forwarded); `-e MY_TOKEN` present without `=`; the value only in the client env; an argv
scan for every secret value finds nothing; an exec after the secret changes carries the new value;
a name without a value is unset in the script.

## Decisions for the lead
- Proposed D-id: "Docker forward-env cannot re-allow a blocked credential name".

Compare checklist (filled after the rewrite; reopened `tools/environments/docker.py`, `remote_common.py`):
- [x] names only, values in the client env (test: `tests/unit/backends/docker/test_env.py::test_values_only_in_the_client_env`)
- [x] blocked names refused even when forwarded (test: `::test_blocked_name_never_forwarded`)
- [x] per-exec re-injection (test: `tests/unit/backends/docker/test_env.py::test_exec_reinjects_updated_value`)
- [x] forwarded values never frozen into the snapshot, R02.1 (test: `::test_forwarded_values_stay_out_of_the_snapshot`)
- [x] names without a value unset (test: `::test_names_without_value_are_unset`)
- Deliberate differences: blocklist cannot be bypassed; the client env starts from the minimal
  child env instead of the whole process env.
- Not taken: egress proxy env merge (D15).

## Status

Done (2026-10-18).

## Integration (lead)

Merged in the P11 integration (2026-10-18, lead's verify tree on main with P08 wave 1; slices applied A, B, C).
- D-16.120 (forward-env cannot re-allow a blocked name).
- The contract suite now checks every backend for R02.1 and Docker/SSH for values on a client command line (`tests/contract/backends/test_docker_ssh_contract.py`).
