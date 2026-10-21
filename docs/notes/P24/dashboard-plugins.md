# Task: Dashboard plugins (P24-37) Phase: 24 Owner: wave-39 builder U3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `web/src/plugins/registry.ts` (all:
register API, SDK object on window, profile switch), `web/src/plugins/slots.ts` (slot names, slot
registry, `PluginSlot`), `web/src/plugins/usePlugins.ts` (all: manifest fetch, CSS + script
injection, SRI, the 2 s wait, late registration), `hermes_cli/web_server_dashboard.py` (plugin
discovery, manifest entry, `api` path check, mount skip reasons, consent gate — lines 456–610 and
798–860). Research 09 §3.8. Closed before writing.
Evidence: none.

What it does (own words): a plugin can add a page (tab) or fill named slots in the dashboard. A
plugin ships a `dashboard/` folder beside its agent half: `manifest.json`, a JS entry (a plain
script, run as a classic `<script>`), optional CSS, optional Python `api` module. Discovery scans,
in this order, `<home>/plugins/<name>/dashboard/manifest.json` (user — wins on a name clash), the
bundled plugin folder, and `<cwd>/.indus/plugins/<name>/dashboard/` only when
`INDUS_ENABLE_PROJECT_PLUGINS` is truthy (`1/true/yes/on`, the P13 rule). The result is cached per
process; `POST /api/dashboard/plugins/rescan` refreshes it. `GET /api/dashboard/plugins` lists the
manifests (internal fields stripped) of the plugins that are enabled (P13's `plugins.enabled` /
`plugins.disabled`; bundled ones unless disabled; project ones listed but their `api` never
imported).

Manifest: `{name, label, description?, icon? (a Lucide-style PascalCase name; anything else →
"Puzzle"), version?, tab: {path, position?: "end" | "after:<seg>" | "before:<seg>", override?:
<built-in path>, hidden?}, slots?: string[], entry, css?, api?, integrity?}`. Names are
`[a-z0-9][a-z0-9_-]{0,63}`; entry/css/api are relative paths inside the plugin's `dashboard/`
folder (absolute paths and `..` refused; a bad `api` drops only the backend); `integrity` must be an
SRI string (`sha256-|sha384-|sha512-` + base64) or it is dropped; a tab path must be `/<seg>`;
`override` of `/plugins` is refused (the plugin manager page cannot be taken), and `tab.path` of an
existing built-in path without `override` is refused too.

Serving: static assets at `/dashboard-plugins/<name>/<file>` from that plugin's `dashboard/`
folder only — any `..`, backslash, NUL, absolute part or a resolved path outside the folder → 404;
an unknown or disabled plugin → 404. Backend: the `api` module (a file exporting `router`, a FastAPI
`APIRouter`) is imported in-process (D-16.46: no plugin host process) and mounted under
`/api/ext/<name>/` (the prefix D-16.587 fixed; `/api/plugins/…` is the agent-plugin REST of P18-14).
Requests pass the login gate first, then the plugin-route gate: unauthenticated → 401, authenticated
but the plugin disabled/unknown → 404 (R09.4). A plugin's WebSocket goes only through the SDK
helper, which asks the shared ticket route in gated mode and offers the session token as a
subprotocol in loopback mode — never a credential in the URL (R09.2).

Frontend: before React renders, `window.__INDUS_DASHBOARD_SDK__` (React, hooks, our UI primitives,
the API client, an authed fetch, the WS helper, utils, version) and `window.__INDUS_PLUGINS__`
(`register(name, Component)`, `registerSlot(name, slot, Component)`) are placed. For each manifest
the shell injects the CSS, then a `<script>` with `integrity` + `crossorigin` when the manifest has
one (a mismatch means the browser never runs it); it waits up to 2 s after the scripts were added
before finishing its first render; a plugin that registers later still appears (the registry
notifies); a load error or a script that never registers only warns (shown on the plugin's page as a
note). `override` replaces a built-in page; ordinary tabs are inserted by position; hidden plugins
only fill slots. Shell slots: `backdrop, header-left, header-right, header-banner, sidebar,
pre-main, post-main, footer-left, footer-right, overlay`; page slots `<page>:top` / `<page>:bottom`
for sessions, analytics, logs, cron, skills, plugins, config, env, chat.

Inputs → outputs: plugin folders → `GET /api/dashboard/plugins` JSON; asset requests → file or 404;
`/api/ext/<name>/…` → plugin router or 401/404; manifests → tabs, overrides, slot contents.

Files: `src/indus/web/plugins.py`, `tests/contract/web/test_dashboard_plugins.py`,
`tests/fixtures/web/plugins/` (sample manifests and bundles, ours), `apps/web/src/plugins/registry.ts`,
`apps/web/src/plugins/registry.test.ts`, `apps/web/src/plugins/slots.tsx`,
`apps/web/src/plugins/sdk.ts`, `apps/web/src/plugins/usePluginState.ts`,
`apps/web/src/plugins/shell.test.tsx`. Shared: `src/indus/web/app.py` (catalogue, plugin-route gate
callable, api mounts), `src/indus/web/routes/__init__.py` (one ROUTERS line),
`src/indus/service/ws_auth.py` (`WEB_PLUGIN` purpose) + `src/indus/web/state.py` (it is always
on; the plugin-route gate decides), `tests/contract/web/test_route_list.py` (rescan is an unscoped
write) + `apps/web/src/test/routes.generated.ts` (regenerated), `apps/web/src/shell/routes.ts`
(`PageRoute.page` takes a plugin component), `apps/web/src/App.tsx` (plugin routes, the deep-link
wait, shell and page slots), `apps/web/src/main.tsx` (SDK on `window` before render).

Edge cases / failure modes:
- unreadable or non-JSON manifest → skipped with a warning, others still listed;
- two sources with the same name → the first (user) wins;
- `tab.position` with an unknown segment → appended at the end;
- a plugin's `api` import fails (syntax error, no `router`) → logged, the plugin's assets and tab
  still load, its `/api/ext/<name>/` answers 404;
- rescan after enabling a plugin → its api is mounted then; after disabling → the gate answers 404
  at once (the route stays mounted but unreachable);
- profile switch on the dashboard → registry cleared and the new profile's manifests loaded.

Our design differences / decisions:
- Plugin API prefix `/api/ext/<name>/` (D-16.587) instead of `/api/plugins/<name>/`.
- Window globals named `__INDUS_DASHBOARD_SDK__` / `__INDUS_PLUGINS__`.
- One profile per dashboard backend for discovery (multi-profile backends see the launch profile's
  list; a per-profile plugin host is not taken, D-16.46).
- No Lucide dependency: the icon name is normalised (unknown → `Puzzle`) and passed through; the
  sidebar shows labels only today.

Tests we will write:
- contract: discovery order + user wins; project plugins only with the env opt-in; manifest
  normalisation (bad name, bad tab path, `/plugins` override refused, built-in clash refused, bad
  SRI dropped, unknown icon → Puzzle, absolute/`..` api dropped); `../` in an asset path → 404;
  asset of a disabled plugin → 404; disabled plugin API without auth → 401, with auth → 404;
  enabled plugin API reachable; rescan picks up a new plugin; list strips internal fields.
- web: register before/after the 2 s wait (late registration still renders); `/plugins` override
  refused client-side too; position after/before; slots render in registration order and re-render
  on late registration; SRI integrity set on the script element and a load error only warns; the SDK
  WS helper never puts a credential in the URL.

## Decisions for the lead
- Plugin sockets: purpose `web-plugin` (new in `DASHBOARD_PURPOSES`); a plugin's backend wraps its
  socket endpoint in `guarded_ws(WEB_PLUGIN)`. Follow-up: the plugin-route gate closes a socket to
  a plugin that is off *before* the upgrade (an HTTP 403), so an anonymous caller can tell "off" from
  "on" there; R09.4 holds for HTTP. Accept-then-close 4404 after the credential check if wanted.
- First render: the shell waits (up to 2 s) only for a deep link to a non-built-in path or a page a
  plugin overrides; other pages render at once and plugin tabs appear when they register.
- Shell slots wired: backdrop, header-banner, sidebar, pre-main, post-main, overlay and
  `<page>:top/bottom`; header-left/right and footer-left/right are listed in the SDK but the shell
  has no render point for them yet.
- Asset URLs carry no profile segment: the dashboard's plugins are the launch profile's.
- Project-local plugins are listed (assets served) only with the opt-in; their `api` is never
  imported (the P13 rule that project plugins never run code without a session's consent).

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] discovery order user → bundled → project (opt-in `1/true/yes/on` only), first name wins (test: `tests/contract/web/test_dashboard_plugins.py::test_user_copy_wins_and_project_needs_the_opt_in`)
- [x] manifest fields + defaults (label, icon Puzzle, version 0.0.0, entry dist/index.js, tab path /<name>, position end) (test: `::test_manifest_defaults`, `::test_listing_shows_enabled_plugins_without_internal_fields`)
- [x] api path must stay inside `dashboard/`; a bad one drops only the backend (test: listing `sneaky`)
- [x] mount skip reasons: disabled always wins; user needs enabled; bundled on unless disabled; project never imported (tests: `::test_not_enabled_and_disabled_plugins_are_not_listed`, `::test_project_plugin_api_is_never_imported`)
- [x] 404 only after auth; traversal → 404 (tests: `::test_disabled_plugin_is_401_without_auth_and_404_with_it`, `::test_asset_traversal_and_unknown_files_are_404`)
- [x] SDK on window before render; CSS then script; SRI + crossorigin; 2 s wait; late registration; load failures only warn; NO_REGISTER (tests: `apps/web/src/plugins/registry.test.ts`)
- [x] `/plugins` cannot be taken (server and client) (tests: `::test_manifest_rules`, `registry.test.ts::refuses an override of /plugins`)
- [x] slot names shell + page (test: `registry.test.ts::lists the shell slots…`)
- [x] plugin WebSocket only through the shared helper (tests: `::test_plugin_socket_goes_through_the_shared_helper`, `registry.test.ts::sdk sockets`)
- Deliberate differences: `/api/ext/<name>/` (D-16.587); `tab.override` must name a built-in page;
  a tab path equal to a built-in page without `override` is refused; slot names must look like slots.
- Not taken: per-profile plugin hosts, `@profile` asset URLs, dev cache-busting, a sessionStorage
  manifest cache.

## Status

Done (2026-10-21). Shared files listed under Files.
