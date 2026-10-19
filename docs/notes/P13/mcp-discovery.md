# Task: Discovery and registration (P13-05) Phase: 13 Owner: wave-16 builder N1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/mcp_tool_discovery.py`
(concurrency cap, pass budget, lazy-from-cache, startup summary), `tools/mcp_schema_cache.py`
(cache entry shape, fingerprint), `tools/mcp_tool_registration.py` (toolset per server, alias,
utility selection; skimmed for those three points only). Ours: `mcp/config.py`, `mcp/naming.py`,
`mcp/lifecycle.py` (M1's connection API), `tools/registry.py`, `tools/toolsets.py`. Evidence: none.

What it does (own words): `McpHost` is the MCP client of one profile (one scope). It owns one
`ServerHandle` per configured server: the `ManagedServer` that keeps the connection, the tools the
server registered, the read-only hints frozen at the last listing, the server's call lock and its
breaker. `discover()` brings every enabled server that is not running yet up, at most
`mcp.discovery_concurrency` (4; 0 = all) connecting at once. Each server gets 120 s inside its
wave; the whole pass is bounded by min(120 s × waves, 300 s). A server that misses its budget keeps
trying in the background (its `ManagedServer` ladder) and registers itself when it does connect,
because registration runs on every (re)connect through `on_ready`.

Registration (on every connect): `tools/list` (≤ 50 pages, bounded by the server's
`connect_timeout`) → `NameFilter` → `naming.tool_schema` → registry overlay of the profile under
toolset `mcp-<server>`; tools the new listing no longer has are deregistered (no wipe and
re-register). Every server also gets a toolset alias `<server>` → `mcp-<server>`; when the name is
also a built-in toolset (`web`, `browser`, …) the alias unions the two. A server whose tools are all
filtered out registers nothing (no toolset, no alias). Utility tools
`mcp__<server>__{list_resources,read_resource,list_prompts,get_prompt}` only when
`tools.resources` / `tools.prompts` allow it **and** the server advertises that capability; a server
tool that normalises onto a utility name wins over the utility.

Schema cache: `<home>/cache/mcp/<server>.json` = `{fingerprint, tools: [{name, description,
inputSchema, readOnlyHint}], utilities: [...], written_at}` written (atomically, 0600) on every live
listing. Fingerprint = sha256 of the connection-defining config (command, args, url, transport,
tool filters). `lazy: true` with a cache entry of the same fingerprint → tools registered from the
cache, nothing spawned; the first call starts the server (P13-06 path) and the live listing then
replaces the cached one (phantom cached tools are deregistered). Lazy with no entry or a stale one
→ connects eagerly once (refreshing the cache) and stays up.

Status: `host.status()` → one `ServerReport` per configured server with state `ready`, `lazy`
(registered from cache, "starts on first use"), `connecting`, `reconnecting`, `cooling_down`,
`parked`, `recycled`, `disabled`, `failed`, `closed` and tool counts. Startup summary
(`DiscoveryReport.summary()`): tools and servers connected, every failed server with its error,
those skipped while cooling down with the time left, lazy servers not spawned.

Inputs → outputs: (scope, `ToolRegistry`, clock, `ConnectOptions`, opener) → `McpHost`;
`discover()` → `DiscoveryReport`; registry overlay entries; cache files.

Files: `src/indus/mcp/discovery.py`, `src/indus/mcp/schema_cache.py` (new, small),
`tests/unit/mcp/test_discovery.py`; shared: `src/indus/tools/registry.py` (toolset aliases),
`src/indus/mcp/connect.py` (raw `readOnlyHint` tap, P13-07).

Edge cases: a server that never answers (ends its wave at 120 s, keeps trying in the background);
a whole pass of hanging servers (300 s); disabled server (listed as disabled, never spawned);
broken config entry (reported as a problem, not attempted); server still cooling down from an
earlier pass (skipped, time left reported); two server tools normalising to the same name (both
refused: we cannot tell which one the model means); a registry name owned by another toolset
(refused, the owner keeps it); a cache file that is unreadable or for another fingerprint (eager).

Our design differences / decisions:
- Registration rides on `ManagedServer.on_ready` (every connect), so late and reconnected servers
  refresh themselves; the reference registers once per discovery pass and re-lists on reconnect
  separately.
- One cache file per server (row) instead of one shared file.
- The cache also stores the raw read-only hint so a lazy server's trust gate works before it
  connects (fail closed when absent).
- Tools of a parked or cooling-down server stay registered (stable tool array, R01.1); calls get
  the breaker / unavailable result instead of the tool vanishing mid-session.
- Tool-search bridge (C4) does not exist yet: MCP tools are recognisable by the `mcp__` prefix and
  the `mcp-` toolset; the deferral flag is left for the bridge task (follow-up for the lead).

Tests we will write: ten fake servers, never more than 4 connecting (probe counter); a hanging
server ends its wave at 120 s and the pass at 300 s (fake clock); filtered-out server creates no
toolset; lazy server registers from cache without spawning; changed command forces eager; startup
summary lists failures and cooling-down skips; alias unions with a built-in toolset; utilities only
when allowed and advertised; disabled server never spawned; phantom cached tool removed after the
lazy server connects.

## Decisions for the lead
- Tools stay registered while their server is parked / cooling down (see above).
- Per-server toolset alias `<server>` → `mcp-<server>` lives in the registry overlay of the profile
  (new `ToolRegistry.set_toolset_alias`; `names_in_toolset` follows aliases).
- Host wiring (who builds the `McpHost` per profile and runs `discover()` at session start) is the
  lead's: `McpHost(scope, registry, clock=…)`, `await host.discover()`, `hosts.put(host)`.

## API for N2 (`indus mcp`, OAuth, sampling) and the lead

```python
from indus.mcp.discovery import McpHost, register_host, host_for, drop_host
host = McpHost(scope, registry, clock=..., options=ConnectOptions(...),  # auth / session_kwargs (N2)
               credential_stamp=stamp_fn)   # server -> token revision (P13-09), for auth parking
register_host(host)                          # one per profile; reload.reload(scope) finds it here
report = await host.discover()               # DiscoveryReport: connected, lazy, failed, cooling_down,
                                             #   disabled, problems, tools; .summary() startup line
host.status() -> list[ServerReport]          # name, state (ready|lazy|connecting|reconnecting|
                                             #   cooling_down|parked|recycled|disabled|failed|
                                             #   not_started|closed), kind, tools, error, detail,
                                             #   next_attempt_in
host.handles[name].specs                     # registry name -> ToolSpec(server_tool, kind, read_only)
host.reauthorised(name)                      # after `indus mcp login`: wake an auth-parked server
host.listeners.append(fn)                    # gets reload.ToolSurfaceChanged
await host.remove(name) / await host.close()
```
Calls go through the registry (`dispatch`); a plugin's `call_mcp` can use
`calls.call(host.handles[server], spec, args, CallContext(...))`. OAuth (P13-09) must call
`trust.interactive_auth_allowed()` before opening a browser and run its login inside
`trust.allow_interactive_auth()`.

Compare checklist (filled after writing; reopened `tools/mcp_tool_discovery.py`,
`tools/mcp_schema_cache.py`, `tools/mcp_tool_registration.py`):
- [x] concurrency 4 / 0 = unlimited (`tests/unit/mcp/test_discovery.py::test_never_more_than_four_servers_connect_at_once`, `::test_concurrency_zero_means_all_at_once`)
- [x] 120 s per wave, pass min(120 × waves, 300) (`::test_a_hanging_server_ends_its_wave_at_120_s`, `::test_the_whole_pass_ends_at_300_s`)
- [x] list paging ≤ 50 pages (P13-03 `LiveSession.list_tools`)
- [x] toolset `mcp-<server>`, alias per server name, none when everything is filtered (`::test_tools_register_under_the_server_toolset_with_an_alias`, `::test_a_server_named_like_a_builtin_toolset_unions_with_it`, `::test_a_fully_filtered_server_creates_no_toolset`)
- [x] utilities need config + advertised capability; native tool of the same name wins (`::test_utilities_only_when_allowed_and_advertised`)
- [x] lossy normalisation collisions fail closed (`::test_two_tools_that_normalise_to_one_name_are_both_refused`)
- [x] lazy from cache without spawning, first call starts it, phantom cached tools dropped (`::test_lazy_server_registers_from_cache_without_starting`); stale fingerprint → eager (`::test_an_edited_lazy_server_connects_eagerly_once`)
- [x] startup summary names each failed server and the ones skipped in cooldown (`::test_startup_summary_names_failures_and_cooling_down_servers`)
- [x] parked servers nudged by a new pass unless auth-parked with unchanged credentials (P13-07 test)
- Deliberate differences: registration on every connect (`on_ready`); one cache file per server; the
  cache keeps the raw read-only hint; the fingerprint also covers `tools.resources/prompts`; no
  `ttlMs` expiry of cache entries (a stale lazy manifest is replaced at the first call); tools of
  parked servers stay registered; no `-t` spawn filter (the reference skips servers outside the
  `--toolsets` list) — follow-up for the CLI host wiring.
- Not taken: cross-profile connection adoption, the safe-worker opt-out, GIL switch-interval tuning,
  deferred background start for the desktop.

## Status

Done (2026-10-19). `src/indus/mcp/discovery.py`, `src/indus/mcp/schema_cache.py`,
`tests/unit/mcp/test_discovery.py`, `tests/fakes/mcp/scripted.py`,
`tests/contract/mcp/test_host_live.py`; shared: `src/indus/tools/registry.py` (aliases).
