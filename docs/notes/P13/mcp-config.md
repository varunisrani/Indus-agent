# Task: MCP server config (P13-01) Phase: 13 Owner: wave-10 builder M1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/mcp_tool_config.py` (safe env
keys, `${...}` interpolation, context variables, hidden-whitespace warning, remote fail-closed on an
unset reference), `tools/mcp_tool_common.py` (the `env:` prefix helper, lifecycle seconds parsing),
`website/docs/reference/mcp-config-reference.md` (skim: key table, `${env:VAR}` form, context
variables, `include`/`exclude` as string or list). Research `07-learning-skills-plugins-cron.md`
§3.16.1, §3.16.2, R07.22, R07.25. Ours: `src/indus/safety/child_env.py`,
`src/indus/foundation/config/{schema,loader,writer}.py`, `src/indus/foundation/scope.py`.
Evidence: none.

What it does (own words): every entry under `mcp_servers.<name>` in the profile config becomes a
typed `McpServer` (Pydantic). A server is stdio (`command` + `args`, optional `env`, `cwd`) or
remote (`url`, `transport: http | sse`, `headers`, OAuth, mTLS, identity header). Shared knobs:
call timeout 300 s, connect timeout 60 s, `lazy`, stdio recycling limits (0 = off), `enabled`,
parallel calls, the tool filter, the trust tier, sampling and elicitation blocks. A broken entry is
reported with its reason and skipped; the other servers still load. `mcp.{auto_reload_on_config_change,
discovery_concurrency}` is a new config section.

Placeholders: `${NAME}` and `${env:NAME}` are resolved when a connection is made, never at load
(the config loader may already have expanded names present in the env view; names that only live
in the secret store, and the context variables, are left for us). Lookup order: the scope's secret
store, then the scope's env view. Context variables (`${userHome}`, `${workspaceFolder}`,
`${workspaceFolderBasename}`, `${pathSeparator}`, `${/}`) are matched case-sensitively, so
`${WORKSPACEFOLDER}` is an ordinary env name. An unset name stays literal for stdio servers (the
server reports what it misses); for a remote server an unset name in the URL or a header refuses
the connect with an error naming the variable, because sending `${TOKEN}` literally only produces a
confusing 401.

Stdio env: built only through `safety.child_env(variant="minimal")` from the scope env (PATH, HOME,
USER, SHELL, TERM, LANG, LC_ALL, TMPDIR) plus `XDG_*` names and the Windows location names
(SYSTEMROOT, APPDATA, …; matched without case) taken from the scope env, then the configured `env`
on top. Nothing else from the parent: a parent `OPENAI_API_KEY` never reaches the server (R07.22,
R02.22). `cwd` defaults to the session's working directory when the caller passes one.

Inputs → outputs: config mapping (+ scope, workspace) → `ServerTable` (servers, problems);
`McpServer` + scope → `StdioLaunch {command, args, env, cwd}` or `RemoteEndpoint {url, headers,
transport, cert}` for P13-03.

Files: `src/indus/mcp/config.py`, `tests/unit/mcp/test_config.py`; shared:
`src/indus/foundation/config/schema.py` (+ its test), `src/indus/foundation/errors.py` (codes).

Edge cases / failure modes:
- both `command` and `url`: the URL wins and a warning names the server; neither → problem.
- `trust` missing → `full`; any other spelling than `full`/`untrusted` (`untrsted`, `FULL!`) →
  `untrusted` with a warning (a typo must not switch the gate off, R07.25).
- `include`/`exclude` written as one string → a one-item list.
- negative timeouts → problem; `idle_timeout_seconds`/`max_lifetime_seconds` 0 = never.
- `client_secret_ref` holds a secret *name*; a value-shaped string is refused.
- values with leading/trailing whitespace: one warning per (server, key path), value untouched.
- identity header on a stdio server: ignored with one warning.

Our design differences / decisions:
- Our names (`McpServer`, `ServerTable`, `StdioLaunch`, `RemoteEndpoint`).
- Placeholders are resolved in command, args, env values, cwd, url, header values, the static
  identity value and the cert paths (the row lists command/args/url/headers; env values are the
  common case for tokens, so they are included).
- Left out: plugin-provided ("portable") servers, the suspicious-entry filter, the npx cache
  shortcut and the managed launcher swap (no package manager layer here), `ssl_verify` (not in the
  row), `keepalive_interval` (fixed 180 s for remote, see P13-04).

Tests we will write:
- `${VAR}` from secrets beats env; unknown stays literal; remote unset → error naming the var.
- `${workspaceFolder}` vs `${WORKSPACEFOLDER}`; `${/}`; basename.
- stdio env lacks a parent `OPENAI_API_KEY`, keeps PATH/XDG_*/configured values.
- trust parsing table; filter string form; problems for a broken entry; defaults.
- round trip through the config writer keeps a comment next to an `mcp_servers` entry.

## Decisions for the lead
- `${VAR}` lookup order is secret store first, then env view (same order as `child_env`
  passthrough). Easy to flip in `config.lookup_ref`.

Compare checklist (filled after the rewrite; reopened `tools/mcp_tool_config.py`,
`tools/mcp_tool_common.py`, the config reference page):
- [x] `${VAR}` / `${env:VAR}` from the scope only, secrets first (test: `tests/unit/mcp/test_config.py::test_secret_store_beats_the_env_view_and_env_prefix_works`)
- [x] unset stays literal on stdio, remote url/headers refuse naming the variable (`::test_unset_names_stay_literal_and_are_listed`, `::test_unset_variable_in_url_or_headers_refuses`)
- [x] context variables case-sensitive, `${/}`, basename (`::test_context_variables_are_case_sensitive`)
- [x] stdio env: safe baseline + `XDG_*` + Windows names + configured env, no parent `OPENAI_API_KEY` (`::test_stdio_env_is_the_safe_baseline_plus_configured_values`; end to end `tests/contract/mcp/test_transports.py::test_stdio_env_is_filtered_and_configured_values_arrive`)
- [x] unknown trust value → untrusted, missing → full (`::test_trust_tier_fails_closed`)
- [x] include/exclude as one string or a list (`::test_filter_lists_accept_one_string`)
- [x] hidden whitespace warned once per key path, value never logged (`::test_padded_values_warn_once_per_path`)
- [x] round trip through the config writer keeps comments and `${...}` (`::test_round_trip_through_the_config_writer_keeps_comments`)
- [x] gap found: a negative or non-numeric recycle limit skipped the whole server; the reference warns and ignores it — fixed by a lenient validator (`::test_odd_optional_values_warn_and_switch_off`); same for a non-list `include`/`exclude`.
- Deliberate differences: the reference interpolates every string of the entry; we fill the fields a connect uses (command, args, env values, cwd, url, header values, static identity value, cert paths). The reference also forwards names injected by an external secret source and two board variables into stdio children; we pass only the configured `env` (operators list what a server needs).
- Not taken: the nested `lifecycle:` mapping for the recycle limits (reported as an unknown key), plugin-provided servers, the suspicious-entry filter, the npx cache shortcut and managed launcher swap, `ssl_verify`, `keepalive_interval`.

## Status

Done (2026-10-19). `src/indus/mcp/config.py`, `tests/unit/mcp/test_config.py`. Shared: `foundation/config/schema.py` (`McpSection`, `mcp_servers`) + its pinned-defaults test, `foundation/errors.py` (eight `mcp.*` codes). Hypothesis is not in the dev group, so property checks are seeded tables (repo convention).
