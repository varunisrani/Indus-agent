# Task: Vendor credentials and env hygiene (P21-11) Phase: 21 Owner: P21 slice B1 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/remote_common.py`
(`resolve_passthrough_env`, `prepend_unset`, `client_env_with`),
`tools/environments/base_session_env.py` (`_export_dump_excluding_session_vars`). Closed before
writing. Evidence: none.

What it does (own words): the cloud backends need the user's vendor tokens (Modal token pair,
Daytona API key, Vercel token or OIDC token) to talk to the vendor, and those tokens must never
reach anything Indus starts: terminal commands, background processes, MCP servers, code execution,
or the sandbox itself (R02.22). Variables the user chose to forward (`terminal.env_passthrough`)
reach a cloud sandbox only through the SDK's own `env` parameter (or, for Apptainer, the client's
`APPTAINERENV_*` environment), never as text in the command line (R02.7), and are left out of the
shell snapshot together with the per-session bridged variables, exactly as on the local backend
(R02.1).

Inputs → outputs (API B2 builds on):
- `VENDOR_TOKEN_NAMES: Mapping[str, tuple[str, ...]]` per backend (`modal`, `daytona`,
  `vercel_sandbox`); `vendor_token_names() -> frozenset[str]` (all of them).
- `register_vendor_names()` → `safety.child_env.register_blocked_names("sandbox vendors", …)`
  (idempotent; the host calls it at startup next to the provider registration). The same names are
  also in child_env's static floor, so they are stripped even before registration (a test keeps the
  two in step).
- `vendor_credentials(scope, backend) -> VendorCredentials(values, missing)`: each declared name
  from the scope's secret store, else its env view (D-16.22); `missing` lists what is absent for the
  backend's required set (`modal`: both token names; `daytona`: `DAYTONA_API_KEY`; `vercel_sandbox`:
  `VERCEL_TOKEN` + `VERCEL_PROJECT_ID` + `VERCEL_TEAM_ID`, or `VERCEL_OIDC_TOKEN` alone).
  `.reason(backend)` → a human requirement reason or None. Values are handed to the SDK as
  arguments by the backend, never put in `os.environ`.
- `passthrough_plan(scope) -> PassthroughPlan(values, unset, names)`: accepted passthrough names
  (blocked names, vendor tokens included, never pass), values read fresh from the secret store or
  the env view; names with no value go to `unset`.
- `sdk_exec(scope, script) -> SdkExec(command, env)`: the script with an `unset …` line for the
  names without a value, and the values in `env` for the SDK's env parameter. The command text holds
  names only.
- `snapshot_excluded_names(scope) -> tuple[str, ...]`: the passthrough names to keep out of the
  snapshot (the per-session names are always excluded by the P05 wrapper).

Files: `src/indus/backends/vendor_env.py`, `tests/unit/backends/test_vendor_env.py`,
`tests/golden/backends/snapshot_script.sh`; `src/indus/safety/child_env.py` (shared: three floor names).

Edge cases / failure modes:
- A vendor token listed in `terminal.env_passthrough` is refused (warned once by child_env) and
  never forwarded.
- A passthrough name with no value now: unset in the command, so a value frozen in a long-lived
  sandbox cannot leak into this session.
- A secret store that raises is treated as "no value" for that name (debug log, no crash).
- Names that are not shell identifiers are dropped from the `unset` line.

Our design differences / decisions:
- `safety/child_env.py` cannot import `vendor_env` (safety sits below backends in the layer
  order), so its static "sandbox vendors" floor mirrors the declarations (three names added:
  `DAYTONA_JWT_TOKEN`, `MODAL_OAUTH_CLIENT_SECRET`, `MODAL_OAUTH_REFRESH_TOKEN`) and a test pins
  floor ⊇ `vendor_token_names()`. `register_vendor_names()` registers them as well, so a future
  name added here is blocked from startup even before the floor catches up.

Tests we will write:
- a local child's env (default, code and minimal variants) holds no vendor token, even when the
  scope env has all of them and the names are listed for passthrough
- fake Daytona SDK exec: passthrough values in its `env` argument, none in the command text; a name
  without a value unset in the text
- snapshot bootstrap script for SDK backends unsets the session vars and the passthrough names
  (golden script)
- credentials: secret store first, env second; missing names and reasons per backend

## Decisions for the lead
- Wiring: call `vendor_env.register_vendor_names()` at service start (next to
  `register_blocked_names("providers", …)`), or from P21-12's registration of the four backends.

Compare checklist (filled after the rewrite; reopened `tools/environments/remote_common.py`,
`tools/environments/base_session_env.py`):
- [x] Passthrough values resolved per call; a name the scope lacks is unset (test:
  `test_vendor_env.py::test_sdk_exec_puts_values_in_env_not_text`)
- [x] Session vars unset before `export -p` in a subshell (golden:
  `tests/golden/backends/snapshot_script.sh`)
- [x] Blocked names never forwarded (test: `::test_vendor_token_in_passthrough_is_refused`)
- Deliberate differences: no operator "explicit forward" bypass for SDK backends (Docker keeps its
  own `docker_forward_env`); credentials come from the secret store before the env.
- Not taken: the reference's `.env` loader fallback (our scope env view already includes it).

## Status

Done (2026-10-19).

Integrated (lead, 2026-10-19): `register_vendor_names()` runs in `cli/entry.py` right after
`register_blocked_names("providers", …)`; the SDK flow (`sdk_shell`) hands `passthrough_plan` values
to each vendor's exec env parameter, unsets value-less names in the script and keeps every forwarded
name out of the snapshot.
