# P13 wave 2 — slice reports (base ad24cb5)

## N1 — w16-p13n1.patch — DONE (P13-05..08 MCP discovery/registration, calls, trust+breaker, reload)
- New: mcp/{discovery,calls,trust,reload,schema_cache}.py, safety/approvals/tool_gate.py, tests
  (unit/mcp, contract/mcp/test_host_live.py, tests/fakes/mcp/scripted.py), notes ×4.
- Shared: mcp/connect.py (LiveSession.raw_hints — wire readOnlyHint), tools/registry.py
  (set_toolset_alias; names_in_toolset/toolset_names follow aliases), plugins/hooks.py (plugin
  `approve` → tool_gate), config approvals.mcp_reload_confirm (+test), traps R07.22–25, ACC-G14-1.
- Wiring: per profile McpHost(scope, registry, clock, options, credential_stamp) → register_host →
  await host.discover(); CLI starts ConfigWatcher(host); listener on ToolSurfaceChanged drops the
  provider prompt cache and rebuilds the tool array at next session/compression; tui_bridge
  methods_config MCP_RELOAD = "indus.mcp.reload:reload"; /reload-mcp = reload_mcp(host, ask=…).
- API for N2: host.status(), host.handles[name].specs, host.reauthorised(name); calls.call(handle,
  spec, args, CallContext); OAuth checks trust.interactive_auth_allowed() and logs in inside
  trust.allow_interactive_auth().
- Decisions: YOLO / approvals.mode off bypass the untrusted gate; unattended follows P05 keys (deny);
  once/session/deny; parked or cooling servers keep tools registered (calls get unavailable/breaker
  result); no handler retry; reload reconciles without a session message; 2 s list_changed window;
  cache fingerprint covers resources/prompts settings and stores raw hints.
- Follow-ups: tool-search bridge (C4) marks mcp__ tools deferrable; -t/--toolsets spawn filter (all
  enabled servers start); SECURITY: `MEDIA:` lines inside server text are not neutralised — the P15
  delivery scanner must deliver trusted paths only (or neutralise here); a non-boolean readOnlyHint
  makes the SDK reject the whole listing; no cache expiry (ttlMs ignored).

## N2 — w16-p13n2.patch — DONE (P13-09..12 OAuth, sampling+elicitation, OSV preflight, `indus mcp`)
- New: mcp/{oauth,sampling,osv,extras}.py, cli/commands/mcp.py, tests (unit/mcp, cli test_mcp),
  fakes tests/fakes/mcp/{oauth_server,asking_server}.py, notes ×4.
- Shared: providers/auth/pkce.py (keeps `iss`, RFC 9207); providers/auxiliary.py call_route gains
  temperature, tools; foundation/errors.py codes mcp.auth_required / oauth_failed / malware_refused;
  config mcp.osv_preflight (+test); cli/app.py `mcp` row; contract non_tty_table + help catalogue;
  traps R07.22/24, ACC-G19-1.
- Wiring onto N1: per server `indus.mcp.extras.extras_for(name, server, scope,
  sampling=aux_backend(aux))` → pass extras.options + extras.opener() to ManagedServer; P13-06 call
  path wraps each tools/call in `extras.requesters.serving(port, unattended=…, turn_id=…)`;
  /reload-mcp rebuilds extras; P13-07 uses McpAuthRequired for the parked warning; /mcp and
  /reload-mcp chat commands can use status_rows / status_lines.
- Decisions: only explicit logins interactive; in-process refresh fence, store re-read for
  cross-process logins; secret names MCP_OAUTH_<SAFE>_<sha8>_*; oauth.scope / client_metadata_url read
  as extra keys; sampling advertises tools when max_tool_rounds > 0; elicitation returns values;
  unversioned packages checked by name; `add` has no checklist; unknown server in `mcp test` exits 1.
- Follow-ups: drop dead dynamic registration on invalid_client; guard the SDK's guessed /register;
  login clears old tokens before the browser step (reference snapshots/restores — consider fixing);
  no own CIMD document; sampling deprecated from protocol 2026-07-28 (client side kept).

## N4 — w16-p13n4.patch — DONE (P13-25..28 skills hub sources, install pipeline, hub CLI + deep link, project-skill trust)
- New: learning/skills/hub/{__init__,lock,sources,install}.py, learning/skills/project_trust.py,
  cli/commands/skills_hub.py, tests (hub, project trust, CLI), fixtures tests/fixtures/{hub,repos/
  project_skills}/ (contains an inert dangerous-skill fixture for the scanner), notes ×4.
- Shared: learning/skills/{resolver (project tier, quarantined, SkillQuarantined), usage (hub_names
  reads hub lock; HUB_LOCK_FILE import), invoke (trust_of), prompt_index ([project] tag)}.py;
  cli/commands/skills.py (register_hub_verbs; `project` source); cli/slash/learning_cmds.py (/skills
  search|install); service/commands/registry.py; cli/launch.py + cli/repl/banner.py (project skills
  trust question + banner row); config skills.hub.*, skills.trusted_project_dirs,
  skills.project_discovery (+test); non_tty_table (15 rows); traps R07.26; ACC-F11-1, F12-1, F26-1/2,
  F30-1; P09 test helpers write real lock entries (record_hub).
- Lock format: <home>/skills/.hub/lock.json {"version":1,"installed":{name:{…, trust, verdict, …}}};
  unknown trust = community.
- Wiring: P14 cron wraps runs in project_trust.bind_project_cwd(job.workdir); P20 desktop calls
  skills_hub.deep_link_install(scope, url, confirm_install=…); P15 gateway buttons for /skills install;
  packaging rule for optional-skills/ later.
- Decisions: `skills check-updates` (P09 owns `skills check`); no default taps; hub never overwrites a
  skill it did not install; quarantined SKILL.md renamed to the install name; update --force only
  overwrites local edits; non-TTY install without -y exits 2, blocked exits 1; interactive surfaces
  cli/tui/desktop; a "no" is remembered per root.
- SECURITY — MUST FIX AT INTEGRATION (content_scan, P13-20): a dangerous command inside an inline code
  span (e.g. `chmod -R 777 /etc` in backticks) scans `safe` while the same text unquoted scans
  `caution`. Code spans and fenced blocks must be scanned at least as strictly as prose (skills tell
  the agent to run code). Add tests for inline spans and fenced blocks.
- Follow-up: verify the skills.sh API shape against its docs before 1.0 (no network here).

## N3 — w16-p13n3.patch — DONE (P13-18 prompt sections, P13-19 shell hooks + `indus hooks`, P13-21..23 plugin install/consent/CLI, pre_llm_call)
- New: plugins/{prompt_sections,shell_hooks,install,consent,session}.py, cli/commands/{hooks,plugins}.py,
  tests (unit/plugins incl. test_pre_llm_call_turn through a real Agent turn; CLI tests), notes ×6.
- Shared: core/{prologue,turn_state,agent}.py (`llm_context` seam on SessionRuntime/AgentOptions),
  core/prompt/builder.py (BuildContext.plugin_sections — empty keeps goldens), config
  plugins.clone_timeout_seconds / auto_update_check_hours / auto_apply, top-level hooks,
  hooks_auto_accept (+test); cli/app.py hooks, plugins groups; contract non_tty + help catalogue;
  traps R07.18–20, R02.22; ACC-G3-1.
- MUST WIRE AT INTEGRATION (nothing runs in a real session otherwise): per session create the HookBus
  and PluginHost; `plugins.session.plugin_options(options, bus, session_id=…, host=…)` and
  `shell_hooks.register_shell_hooks(bus, scope, interactive=…, surface=…, ask=…)` from cli/runtime;
  `--accept-hooks` global flag in GlobalArgs/global_options passed as accept=; `/plugins` slash row
  (plugin_lines(scope), toggle_plugin(scope, name, on)); Installer.maybe_check_updates() from CLI
  start or the maintenance timer; on_session_start / on_session_end call sites in core.
- Decisions (need D-ids): pre_llm_call before turn-start compaction (context rides the user row's
  api_content, protected tail); plugin sections after memory, before skills index (reference: after);
  install token via GIT_CONFIG_* env, not argv; own capability ids (tools.override, llm.provider,
  llm.model, llm.agent, llm.profile, llm.task, platform.actions); consent file / grant ids / section
  frame text ours; exit 126/127 = start failure (pre_tool_call blocks).
- Note: subagents never get llm_context or plugin sections.
