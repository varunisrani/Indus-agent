# Task: Plugin context API (P13-16) Phase: 13 Owner: wave-10 builder M2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/plugins.py` (the
context class's public methods only: tool/hook/command/CLI registration, tool dispatch, message
injection and posting, session cwd, capability probe, MCP call envelope and allow-list, provider
registrars, prompt sections, event emit/subscribe), research §3.13.2 and §3.16.9. Closed before
writing.
Evidence: none.

What it does (own words): the one object a plugin's `register(ctx)` receives. Through it a plugin
adds tools, hooks, slash and CLI commands, skills, providers and prompt sections, calls tools and
allowed MCP servers, asks the host model for a one-shot answer, posts into the conversation it is
running for, and keeps files in its own data folder. Everything is scoped to the plugin and its
profile; registrations are recorded so a failed load can take them back, and the loader can tell
what went live.

Inputs → outputs: `PluginHost(scope, registry, bus, ports=HostPorts(...))` holds what plugins
registered (`commands`, `cli_commands`, `skills`, `providers`, `prompt_sections`).
`PluginContext` methods: `register_tool`, `register_hook`, `register_command`,
`run_command(name, raw_args, **session)` (host side), `register_cli_command`, `dispatch_tool`,
`inject_message(text, *, session_key=None, origin=None)`, `post_message(text)`, `session_cwd()`,
`register_skill`, `register_image_provider` / `video` / `tts` / `stt` / `web_search` / `browser` /
`desktop_driver` / `context_engine` / `terminal_backend` / `approval_transport` /
`model_provider`, `register_prompt_section`, `call_mcp(server, tool, args, timeout=30)`,
`llm.complete(messages, schema=None)`, `has_capability`, `data_dir()`, `secrets.get`,
`setting(key)`, `emit` / `listen`, `run(coro)` (sync plugin code waiting on the host loop).
Call site: `bind_call(CallSite(...))` tells the context which conversation and cwd a command, hook
or tool runs for.

Files: `src/indus/plugins/context.py`, `tests/unit/plugins/test_context.py`.

Edge cases / failure modes:
- `call_mcp`: server not in `plugins.entries.<id>.mcp_allowlist` → `PluginPermissionError` naming
  that key; `*` is not a wildcard; timeout clamped to 1–600 s (non-number → 30); results over
  65,536 chars cut and flagged `truncated`; JSON with `error` → `{ok: false, error}`; no MCP
  client in this host → `{ok: false, error}`.
- slash command: name normalised (lower case, no leading `/`, spaces → `-`); a built-in name or a
  second plugin's name → refused with a warning; handler sync (on the tools pool) or async,
  awaited at most 30 s, then cancelled and answered with a message; a non-text return is turned
  into text; an exception becomes a short error line.
- `post_message` has no target: it only reaches the conversation bound to the current call;
  outside a call → `{ok: false, error}`.
- `inject_message`: exactly one of `session_key` / `origin`; no injector → False.
- `register_skill`: name `^[a-z0-9][a-z0-9._-]*$`, folder with `SKILL.md`; stored as
  `<namespace>:<name>` (namespace = manifest `skill_namespace`, else the plugin key).
- providers: a name from `provider.name` or the argument; same kind and name twice → refused; one
  context engine only.
- prompt sections: id `^[a-z0-9][a-z0-9._-]{0,127}$`, unique, only anchor `after_memory`
  (rendering and freezing are P13-18).
- `data_dir()` → `<home>/plugin-data/<key>/`, created, never inside the install folder.
- `secrets` exposes only `get` through the scope's secret view.
- after a load timeout the context is closed: every later registration is refused (logged).
- `register_tool` with `override=True` but no operator opt-in → `PluginPermissionError`; without
  `override` a name that is already a built-in → warning, not registered.

Our design differences / decisions:
- Our method names (row): `register_desktop_driver`, `register_terminal_backend`,
  `register_prompt_section`, `listen`, `data_dir`, `llm.complete`.
- Host services come in through `HostPorts` (MCP caller, LLM, injector, poster, reserved command
  names); plugins never import host modules. Missing ports answer with a clear "not available".
- Runtime calls (`dispatch_tool`, `call_mcp`, `llm.complete`) are coroutines; sync plugin code
  uses `ctx.run(...)`, which waits on the host loop from a worker thread.
- Not taken here: platform actions, cron identity, subagent lifecycle facade, middleware,
  dashboard/auth providers, locales, secret sources.

Tests we will write:
- a fake plugin registers one of each kind and each is reachable through the host; `call_mcp`
  to an unlisted server raises naming the key, allowed server works, 100 KB result truncated with
  flag, timeout clamp; `data_dir` outside the install folder; `post_message` only reaches the bound
  conversation; slash handler over 30 s cancelled with a message (fake clock); built-in command
  name refused; skill namespacing; one context engine; secrets read-only; closed context refuses.

## Decisions for the lead
- Event names: the host emits `indus.<event>`; a plugin emits a bare name that is published as
  `<plugin key>.<event>`; listening is open to any full name (row says "namespaced `indus.*`").
- `has_capability` reads `plugins.entries.<id>.granted_capabilities` (names or `{name}` maps);
  bundled plugins have `tools.override`. P13-22 owns writing grants.

Compare checklist (filled after the rewrite; reopened `hermes_cli/plugins.py` context methods):
- [x] MCP allow-list default-deny, error names the key, clamp 1–600, 64 KB cap + flag (test: `test_context.py::test_call_mcp_unlisted_server_names_the_key`, `::test_call_mcp_truncates_large_results`, `::test_call_mcp_timeout_is_clamped`)
- [x] override opt-in (test: `::test_tool_override_needs_opt_in`)
- [x] command name normalising and built-in conflict (test: `::test_command_name_clash_with_builtin_refused`)
- [x] command handler bounded at 30 s (test: `::test_slash_handler_over_thirty_seconds_is_cancelled`)
- [x] post only to the calling conversation (test: `::test_post_message_reaches_only_the_bound_conversation`)
- [x] one context engine (test: `::test_one_of_each_registration_is_reachable`)
- [x] closed (abandoned) context refuses registrations (test: `test_loader.py::test_late_registration_after_timeout_is_refused`)
- Deliberate differences: async runtime calls; host ports instead of module lookups; event names
  dotted with the plugin key as the namespace.
- Not taken: platform action verbs (P15), middleware, state facade, on_unload/spawn_task
  supervision (follow-up if a plugin needs background work).

## Status

Done (2026-10-19).
