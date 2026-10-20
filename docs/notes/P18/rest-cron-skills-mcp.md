# Task: REST: cron, skills, plugins, MCP (P18-14) Phase: 18 Owner: wave-30 builder R2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/web_routers/cron.py`
(route list, the owner-profile lookup, trigger, the fire webhook's verify-then-forward contract and
its 200 "gone" / 503 retry answers, blueprints, delivery targets), `hermes_cli/web_routers/skills.py`
(list, toggle, hub search/install/uninstall/update), `hermes_cli/web_routers/mcp.py` (servers list,
add with duplicate check, test with an OAuth-token check, enable, delete, the dashboard OAuth flow
registry, its TTL/cap, the callback that matches `state` in constant time and answers small HTML),
`hermes_cli/web_routers/tools.py` (the toolsets list). F1's client and notes from `w29-f1.patch`
(`docs/notes/P18/{rest-client,page-cron-skills}.md`, `lib/{api,restTypes}.ts`,
`testing/mockApi.ts`): paths and bodies are binding. Research 09 §3.4. Our `cron/{ports,targets,
tool,models}.py`, `service/cron_host.py`, `gateway/http/admin_routes.py` (the API server's job
routes: same backend, run in the background), `learning/skills/{resolver,hub/install,hub/sources}.py`,
`plugins/{install,discovery,consent}.py`, `mcp/{config,connect,oauth,trust,osv}.py`, the CLI
commands `cron`, `skills`, `skills_hub`, `plugins`, `mcp`, `tools` (rules only; `web` may not
import `cli`), W1's `auth/public_paths.py`. Reference closed before writing. Evidence: none.

What it does (own words): the backend of the Cron, Skills, Plugins and MCP pages. Every write goes
through the owning P13/P14 library under the request's profile scope (`write_scope`: on a
multi-profile host a write names its profile).

- **Cron** (P14 backend `cron.ports.backend_for`): list, one, create `{name?, prompt, schedule,
  deliver?, skills?, repeat?}` (schedule in our grammar or a cron line; prompt safety scan and
  delivery-target grammar checked first, as `indus cron create` does), update (same fields, all
  optional), pause, resume, delete, trigger (starts the run in the background and answers 202 with
  the job; 409 while it already runs), delivery targets `{targets: [{id, label, kind: local |
  platform | email, available}]}` (`local` plus each configured platform; available = it has a
  home channel and is not switched off), blueprints (skills whose front matter carries a
  `blueprint` block). Fire webhook: `POST /api/cron/fire` is on the shared public list; it is
  authenticated by its own token, never by the session gate. A signed-in caller mints the token
  with `POST /api/cron/jobs/{id}/fire-token` (`{token, expires_in}`); the token names the profile
  and the job and expires (default 300 s, at most 3,600 s); it is an HMAC-SHA256 over its claims
  with a random per-process key (so a restart ends every token), sent as `Authorization: Bearer`,
  compared in constant time, and never logged or put in a URL. A good token starts the job in the
  background (202); a job that no longer exists answers 200 `gone` (the caller should not retry);
  anything wrong with the token is one generic 401.
- **Skills**: list `{skills: [{name, description, category, enabled, source}]}`; toggle
  `{name, enabled}` writes `skills.disabled` through the config writer; toolsets list (the plain
  toolsets with their tools and whether the CLI selection offers them); hub search (catalogue and
  taps, then the live fallbacks), inspect (fetch into quarantine, scan, report, discard), install
  `{identifier, force?}` (P13-26 pipeline; a blocked verdict is 409 `scan_blocked` with the
  verdict; `dangerous` is never installed, `force` only lifts `caution`), uninstall `{name}`,
  update `{name?}`.
- **Plugins**: installed list with state, version, source and pinned commit; install from git
  `{url, sha}` (owner/repo or a git URL, never a local folder; `sha` must be a full 40-hex commit;
  the P13-20 scan decides; a block is 409 `scan_blocked` with the verdict); enable / disable (an
  enable that needs Python packages or is a memory provider is refused with a code: those need a
  terminal; capabilities the plugin declares stay ungranted and are listed as waiting).
- **MCP**: servers list (env values and URL credentials redacted; `${NAME}` references shown as
  written); add `{name, command | url, args?, env?, auth?}` (secret-looking literal values refused:
  store them on the Keys page and write `${NAME}`); test (connect, list tools, disconnect within
  15 s; an `auth: oauth` server without a stored sign-in answers "sign-in needed" without
  connecting); enable/disable; delete (also removes the stored sign-in); OAuth: start a sign-in
  (`POST /api/mcp/servers/{name}/auth` → `{flow_id, status, authorization_url}`), poll and cancel
  the flow, and the browser's callback `GET /api/mcp/oauth/callback?code&state` (public: the IdP
  redirects a browser that carries no dashboard credential), which finds its flow by `state` in
  constant time and answers a small HTML page that echoes nothing from the request.

Inputs → outputs: JSON bodies → JSON; errors `{detail: {code, message, …}}`; reads `read_scope`,
writes `write_scope` (the fire webhook and OAuth callback are the two public exceptions: they
resolve their profile from the signed token / the flow).

Files: `src/indus/web/routes/{cron,skills,plugins,mcp,mcp_oauth}.py`,
`tests/contract/web/{test_cron_skills_routes,test_plugins_mcp_routes}.py`; shared: ROUTERS lines,
`web/auth/public_paths.py` (the MCP OAuth callback), trap/ACC lines.

Edge cases / failure modes:
- Fire token: expired by one second, wrong signature, other job, other profile, minted by another
  process, malformed, absent → 401 with one body; a deleted job → 200 `gone`; a running job → 409.
- Trigger while running → 409 `job_running`; trigger of a paused job runs it once (the backend
  decides).
- Unknown job / skill / plugin / server → 404 with a code.
- Hub network failure → 502 `hub_unavailable`; the quarantine copy is always removed (library).
- Plugin install of a short or non-hex SHA → 422 before any git call; a local path → 400.
- MCP add with both command and url, or neither → 422; duplicate name → 409; a broken entry in the
  file is listed with its problem instead of failing the list.
- MCP test of a server that hangs → error after 15 s, the process is gone.
- OAuth: at most 8 flows in progress, one per server and profile; flows expire after 15 minutes;
  a callback with an unknown or spent `state` → 404 page; an `error=` callback ends the flow.

Our design differences / decisions:
- Trigger and the fire webhook run the job in the dashboard process, in the background (like the
  P15 API server's `/api/jobs/{id}/run`), because the daemon has no "run job" control verb yet;
  the reference forwards to its gateway.
- The fire token is ours (HMAC, per-process key, minted by an authenticated route) instead of a
  JWT from an external scheduler (no hosted scheduler, L14).
- Hub search results carry `verdict: null` (a search does not download); the verdict comes from
  inspect or install.
- No MCP catalogue, no replace-all route, no per-toolset configuration routes, no skill editor
  routes (not on the 1.0 pages).

Tests we will write: cron CRUD/pause/resume/trigger against the real store; bad schedule → 400;
fire with a good token → 202 and the run starts; expired (injected clock) → 401; tampered,
other-job and missing token → 401; deleted job → 200 gone; fire reachable without the session
token; skills list/toggle round trip through `config.yaml`; hub install of a `dangerous` fixture
→ 409 with the verdict and nothing installed; plugin install short SHA → 422, blocked scan → 409;
enable/disable round trip; MCP add/list (env redacted)/enable/delete; MCP test against the fake
stdio server lists its tools; OAuth callback is public, unknown state → 404, a matching state
reaches its flow; every write route depends on `write_scope` (fire and callback excepted); route
list pinned.

## Decisions for the lead
- Fire token route: `POST /api/cron/jobs/{id}/fire-token {ttl_s?}` → `{token, expires_in,
  path: "/api/cron/fire"}`; webhook body `{job_id}` optional (must match the token when given).
- Delivery targets read the profile config only (`platforms.<name>.home_channel`,
  `platforms.<name>.enabled`); live daemon state is not consulted.
- Blueprints: list only (`GET /api/cron/blueprints`); instantiating one is creating a job with its
  schedule and prompt (the page can prefill the create form).
- Skills toggle is `POST` (F1's client), the reference uses `PUT`.
- Plugin install never enables; enabling is a second call (so dependency and capability consent
  stay with a terminal or a later page).
- MCP OAuth callback path is the exact `/api/mcp/oauth/callback` (the public list matches exact
  paths; the server is found through `state`, not the path).
- `POST /api/mcp/servers/{name}/test` uses `write_scope` (it starts a configured program).
- The dashboard OAuth flow reuses `mcp.oauth`'s private helpers (`_settings`, `_store_for`,
  `_client_metadata`, `_extra`, `_put_back`); a public `oauth.redirect_login(...)` in P13 would be
  cleaner (follow-up).

Compare checklist (filled after the rewrite; reopened `cron.py` (mutations, trigger, fire,
targets, blueprints), `skills.py`, `mcp.py` (servers, test, OAuth flow and callback), `tools.py`
(toolsets list), and the request models in `web_models.py`):
- [x] fire: token checked before the body is read; one 401 for every token problem; gone job →
  200 so the caller stops (tests: `tests/contract/web/test_cron_skills_routes.py::test_fire_for_a_deleted_job_is_gone`, `::test_fire_refuses_bad_tokens_with_one_answer`, `::test_fire_token_expires`)
- [x] trigger: already running → 409 (test: `tests/contract/web/test_cron_skills_routes.py::test_trigger_runs_in_the_background_and_refuses_a_second`)
- [x] CRUD, pause/resume, delete answers `{ok: true}`, unknown job 404 (test: `tests/contract/web/test_cron_skills_routes.py::test_cron_crud_pause_resume`)
- [x] delivery targets: local plus configured platforms (test: `tests/contract/web/test_cron_skills_routes.py::test_delivery_targets_list_local_and_configured_platforms`)
- [x] skills toggle writes the disabled list and answers `{ok, name, enabled}` (test: `tests/contract/web/test_cron_skills_routes.py::test_skill_toggle_round_trips_through_config`)
- [x] hub: dangerous never installed, force or not (test: `tests/contract/web/test_cron_skills_routes.py::test_hub_install_of_a_dangerous_skill_is_409_with_the_verdict`); empty search → empty results (test: `::test_hub_search_marks_no_verdict`)
- [x] MCP add refuses duplicates; enable keeps the entry; delete 404 when unknown (test: `tests/contract/web/test_plugins_mcp_routes.py::test_mcp_add_list_toggle_delete`)
- [x] MCP test: lists tools; failure is `{ok: false, error}`; oauth server without a token → not
  ok without connecting (tests: `tests/contract/web/test_plugins_mcp_routes.py::test_mcp_test_against_the_fake_stdio_server_lists_tools`, `::test_mcp_test_reports_a_failure_without_a_500`, `::test_mcp_test_of_an_oauth_server_without_sign_in`)
- [x] OAuth: stdio and header servers refused; one flow per server (409), at most 8 (429), 15 min
  TTL; callback matches `state` in constant time, answers fixed HTML, a second callback is
  refused, `error=` ends the flow, cancel unblocks it (tests: `tests/contract/web/test_plugins_mcp_routes.py::test_oauth_callback_is_public_and_matches_state`, `::test_oauth_refusal_and_cancel_end_the_flow`, `::test_oauth_needs_a_remote_server`)
- [x] toolsets list: name, label, description, enabled, tools (test: `tests/contract/web/test_cron_skills_routes.py::test_toolsets_list_plain_toolsets_with_tools`)
- Deliberate differences: see above; the callback path carries no server name (exact public
  path), the flow is found by `state` alone; the callback page says nothing about the reference
  product; delete also removes the stored sign-in (as `indus mcp remove`).
- Not taken: `bearer_token` provisioning on MCP add (the page sends `${NAME}` headers; the value
  goes on the Keys page), a per-server `oauth.redirect_uri` override (our OAuth settings have
  none), cron `paused` on create, cron runs history (`/runs`), cross-profile `profile=all` job
  listing, blueprint instantiation and its form schema, official-catalogue / sources / preview /
  scan hub routes, skill content editor routes, toolset configuration routes, terminal-backend and
  computer-use routes, the MCP catalogue and whole-map replace.

## Status

Done (2026-10-20). New: `src/indus/web/routes/{cron,skills,plugins,mcp,mcp_oauth}.py`,
`tests/contract/web/{test_cron_skills_routes,test_plugins_mcp_routes}.py`. Shared:
`src/indus/web/routes/__init__.py` (ROUTERS lines), `src/indus/web/auth/public_paths.py` (the MCP
OAuth callback path), trap R09.4 / R09.20 lines. W1's `tests/unit/web/auth/test_gate.py` pins the
allowlist set and needs the new path added (not edited here: W1 file).

Integration (P18 REST integrator, 2026-10-20): `/api/mcp/oauth/callback` is on the shared public
list (D-16.599; W1's allowlist pin updated); F1's `HubResult.verdict` is now `ScanVerdict | null`
and the Skills page shows "scanned on install" for a search result. Decisions numbered D-16.614
(fire token), D-16.615 (trigger / fire runs), D-16.616 (skills hub, plugins), D-16.617 (MCP).
