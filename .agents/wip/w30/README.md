# Wave 30 parked patches (P19 MCP serve; P18 REST and P19 ACP builders to follow)

## w30-m1.patch: P19-14..17 `indus mcp serve` (read tools, event bridge + long-poll, messages_send, approval tools) — done (base 0ab0d95)
- Builder checks: tests/contract/mcp_serve 34 passed (3 runs); combined contract cli + gateway outbound/delivery/daemon +
  control socket + config + service daemon_host + cli mcp/send/entry + integration control_part 1035 passed (not perf);
  ruff, pyright 0, lint-imports, tooling.checks ok. Full suite not run. No packages (mcp==2.3.0 already pinned).
- New: src/indus/mcp_serve/{server,store,read_tools,event_bridge,send,approvals}.py, cli/commands/mcp_serve.py,
  service/messaging_bridge.py (verb names, approval-queue capability, decision map, client helpers),
  gateway/daemon/bridge_verbs.py (control verbs deliver-message, approvals-open, approvals-respond; owner-only),
  safety/media_policy.py (moved from gateway/outbound/media_policy.py, which now re-exports — layer rule), tests
  contract/mcp_serve/*, notes docs/notes/P19/mcp-serve-{read,events,send,approvals}.md.
- Shared: cli/commands/mcp.py (serve registered), cli/entry.py (STDIO_SUBCOMMANDS {("mcp","serve")} — trivial conflict
  with w29-a1's STDIO_COMMANDS line), gateway/delivery/router.py (send(session_key=, message_ref=) to FinalReply),
  gateway/daemon/side_services.py (BridgeVerbs handlers), gateway/control_socket.py (KNOWN_VERBS/OWNER_ONLY_VERBS),
  service/daemon_host.py (read-only open_requests), contract cli non_tty_table (mcp serve long_running), traps R03.14/16/17,
  R06.22, R09.10, R09.14; ACC-M10-1/2/3, ACC-M11-1; D-16.30 cell points at the approvals note.
- Decisions (number at integration): media policy moved to safety; attachments_fetch returns paths (+ `refused` with
  reasons), inline data: images summarised; baseline = newest message id at start; compaction copies/summary rows not
  events (uid check against session + parent); approval watcher ≤ 1 query/s, prompts open at start announced; messages
  over 16,000 chars refused; malformed target → bad_target; ledger key `mcp-send:<platform>:<chat>` (never the chat's
  conversation key — crash recovery would read a newer row as the turn's owed reply).
- Follow-ups: an approval in a session with no viewer is settled `undeliverable` (P17-07) so the bridge never sees it —
  register the MCP server as an approval viewer if MCP-only approval is wanted; tool list fixed at start (daemon started
  later needs a server restart); user doc for running as an MCP server owed.
- Integrate after P16/P18/P19 land (touches daemon side services, control socket, entry.py).

## w30-r1.patch: P18-11 REST sessions + P18-12 REST config / env keys / effective config — done (base: main 0ab0d95 + w29-w1 + w29-w2)
- Apply AFTER the P18 integration (W1+W2+F1) lands; diff is against W1+W2 only.
- Builder checks: tests/unit/web + contract/web + integration/web + state listing/browse + cli sessions 475 passed (38 new);
  ruff, pyright 0, lint-imports, tooling.checks ok. Full suite not run. No packages.
- New: web/routes/{sessions,sessions_store,config_env,config_view}.py, tests/contract/web/{test_sessions_routes,
  test_config_env_routes,_rest_app}.py, tests/fixtures/store/build_dashboard.py, notes docs/notes/P18/rest-{sessions,config-env}.md.
- Shared: web/routes/__init__.py ROUTERS (+2), state/listing.py (order="activity", count_by_source ~35 lines), traps R03.16,
  R03.22, R04.12, R04.15, R09.21; ACC-L3-1, L5-1.
- Wiring: create_app must call install(app, ProfileScopes(scope)) (else these routes 500; tests install it); test seam
  DashboardState.extras["sessions.gate"]. ROUTE_LIST constant per module ({method, path, scoped}), pinned by a test —
  replaces F1's _stubs_f1.ts entries (config/schema and defaults are unscoped).
- Decisions (number at integration): revision = 31-bit CRC of visible row fields (0 = absent), generation = ledger counter,
  `stale_revision` code; single delete via the maintenance gate's delete op (daemon-side fenced verb would close a small
  race); prune older_than_days >= 1, 422 for bad bodies; dry-run prune needs ?profile= on multi-profile hosts; "recent"
  order uses the row's own activity stamp; config GET never expands ${NAME}, masks credential-shaped literals, PUT restores
  them; effective source `env` = from ${NAME} reference; keys signup url null, DELETE unset → 200 {removed:false};
  credential-name rule restated in config_view.py (layer rule) — move to foundation/config/keys.py.
- Bugs found in W2 (sent to the P18 integrator): scope.unscoped_write_routes misses included routers (FastAPI 0.142
  _IncludedRouter); scope.py errors use {detail:{error,...}} but F1 reads detail.code.
- Follow-ups: REST pydantic models in indus.protocol + generated packages/protocol types; generated route list.

## w30-r2.patch: P18-13 REST status / logs / analytics + P18-14 REST cron / skills / plugins / MCP — done (base: main 0ab0d95 + w29-w1 + w29-w2)
- Apply AFTER the P18 integration lands (diff against W1+W2 only), next to w30-r1.
- Builder checks: unit/contract/integration web 456 passed, 1 failed = expected W1 pin: tests/unit/web/auth/test_gate.py
  ::test_the_allowlist_is_one_object_in_both_gates — integrator adds "/api/mcp/oauth/callback" to its expected set (~line 115).
  102 new tests; ruff, pyright 0, lint-imports, tooling.checks ok. Full suite not run. No packages.
- New: web/routes/{status,logs,analytics,cron,skills,plugins,mcp,mcp_oauth}.py, tests/contract/web/{_rest_app_r2,
  test_status_routes,test_logs_analytics_routes,test_cron_skills_routes,test_plugins_mcp_routes}.py, notes
  docs/notes/P18/rest-{status-logs,cron-skills-mcp}.md.
- Shared: web/routes/__init__.py ROUTERS (+7), web/auth/public_paths.py (+/api/mcp/oauth/callback), traps R09.4, R09.20;
  ACC-L6-1 (no ACC-L7/L11/L12 rows exist).
- Wiring: needs install(app, ProfileScopes(scope)) in create_app (like r1); test seams in DashboardState.extras (cron.backend,
  cron.runner, skills.hub_sources, plugins.installer, mcp.oauth_connect, mcp.oauth_start, status.memory/disk/now); runtime
  extras cron.fire_key (32 random bytes per process), mcp.oauth_flows; ROUTE_LIST per module. Only unscoped writes: POST
  /api/cron/fire (signed token) and public GET /api/mcp/oauth/callback (flow by state). Merge _rest_app_r2.py with r1's
  _rest_app.py at integration.
- Decisions (number at integration): fire token = own HMAC-SHA256 over {profile, job_id, expires_at}, minted by
  authenticated POST .../fire-token, TTL 300 s (max 3,600), per-process key (restart revokes), constant-time, never logged,
  one 401 for every token problem, deleted job → 200 gone; trigger/fire run the job in a dashboard background thread (no
  daemon "run job" verb; P15 API server precedent), 409 when running; status shape our own (see note), "recent 20" = active
  count over the 20 most recent, binary MiB/GiB thresholds, dead daemon record → stopped; analytics days 1..365; hub search
  verdict null; plugin install never enables, enable refused for plugins needing Python packages and memory providers; MCP
  add refuses literal secrets (${NAME} only).
- Follow-ups: public redirect-login function in P13 mcp/oauth.py (mcp_oauth.py uses its private helpers); status shape and
  nullable hub verdict into F1 restTypes (P18-25); not taken: MCP bearer_token provisioning, cron run history, blueprint
  instantiation, logs `search`.
- Slip: one early `uv run python -c` ran before HOME/UV_CACHE_DIR were exported → may have written to /root/.cache/uv (the
  default uv cache the main checkout already uses). Owner item; nothing deleted.

## w30-r3.patch: P18-15 REST channels / webhooks / pairing / daemon control (done) + P18-16 REST profiles / models / files / system (partly: prompt-size breakdown not built) — base: main 0ab0d95 + w29-w1 + w29-w2
- Apply AFTER the P18 integration lands, with w30-r1 and w30-r2.
- Builder checks: unit/contract/integration web 410 passed, 3 failed = W1 tests whose stand-in routes the real routes now
  shadow (ROUTERS before options.routers): test_gate.py::test_query_token_only_on_the_download_route (real download needs
  `path` → 422), test_service_bearer.py::test_a_strong_secret_opens_only_its_route and ::test_the_route_ignores_cookies
  (real drain route needs the profile resolver; with no daemon → 409 daemon_not_running) — integrator adjusts them to assert
  "not 401" or installs a fake control seam. 58 new tests; ruff, pyright 0, lint-imports, tooling.checks ok (one similarity
  hit rewritten). Full suite not run. No packages. No environment slip.
- New: web/routes/{messaging,webhooks,daemon,profiles,models,files,system}.py, web/actions.py (background actions), tests
  contract/web/{test_messaging_routes,test_files_system_routes,test_profiles_models_routes,_rest_app_r3,_fake_control_r3}.py,
  notes docs/notes/P18/rest-{channels,profiles-system}.md.
- Shared: web/routes/__init__.py ROUTERS (+7), traps R04.25, R06.15, R09.20.
- Wiring: install(app, ProfileScopes(scope)) in create_app (as r1/r2); seams in extras (actions.command, daemon.query,
  daemon.ensure, profiles.run, system.install_info, system.git_behind, providers.device_flows, providers.http_client);
  runtime extras actions, providers.login_sessions; ROUTE_LIST per module. Deliberately unscoped writes: POST
  /api/daemon/drain (own profile unless ?profile=), profile writes (profile in path), files writes (machine-level).
  Merge _rest_app_r{,2,3}.py into one builder at integration.
- LEAD DECISION FOR INTEGRATION (security): the files download takes the session token as `?token=` (W1's gate allows the
  query token on that one route). Our rule is no tokens in URLs: replace it with a short-lived (≤ 60 s), single-use,
  path-bound download ticket minted by an authenticated POST (like the WS ticket), and refuse `?token=` everywhere. Record a D-id.
- Decisions (number at integration): daemon stop/restart send a new owner-only control verb `stop` {reason, restart} the
  daemon does not register yet → fails with a message naming `indus gateway stop`, never signals a pid (ADD THE DAEMON VERB
  at integration: planned stop record then normal shutdown); gateway registry/settings and cli.version loaded by module name
  (layer rule); profile create/rename/delete run `indus profile …` as a child process (layer rule), no alias script unless
  alias: true; profile export/import = our own zip, settings only (no history/logs/secrets/.env); pairing routes in
  messaging.py; webhook create allowed while the receiver is off; GET /api/daemon state route added; secret-holding names
  refused for file writes/deletes too; ~ read against the backend scope's HOME; no sudo case (per-user service units).
- Follow-ups: prompt-size breakdown helper below cli (P04) then the route; move profile ops into service; cli/version.py →
  foundation; REST models into indus.protocol.
