# Task: Desktop plugin SDK and contribution registry (P24-32) Phase: 24 Owner: wave-39 builder U3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/src/contrib/plugin.ts`
(all 375 lines: the author contract, scoped context, storage, OS door, lifetime helpers),
`apps/desktop/src/contrib/plugins-store.ts` (all 149: inventory records, decisions, live toggle),
`apps/desktop/src/contrib/runtime-loader.ts` (lines 1–400: the load pipeline, import allow-list,
code-range scan, unload ordering), file list of `apps/desktop/src/contrib/`. Research 08 §3.22.
Closed before writing.
Evidence: none.

What it does (own words): the desktop shell becomes contribution-driven. A **contribution** is a
small record `{id, area, …}` placed in one registry; the shell reads each area from the registry
and re-renders when it changes. Areas: `pane`, `route`, `sidebar-nav`, `status-bar`, `title-bar`,
`palette-command`, `keybind`, `theme`, `composer-extension`, `session-row-decoration`,
`model-menu-decoration`, `appearance-setting`, `plugin-settings-page`, `transcript-directive`.

A **plugin** is a module whose default export is `{id, name?, description?, defaultEnabled?,
register(ctx)}`. It never touches the registry itself: `register` gets a `PluginContext` whose
`register(contribution)` stamps the source (`plugin:<id>`) and namespaces the id (`<id>:<local>`).
Loading is a **transaction**: everything `register` adds is staged; if `register` throws (or a
contribution is invalid, or the per-plugin cap — 200 contributions — is passed) nothing staged
reaches the registry and the plugin is recorded as `error`; on success all of it is committed in
one change. Unload (disable, reload, file deleted) runs every disposer the plugin took out —
contributions, event subscriptions, timers, listeners — each in its own try/catch, after the
plugin is first dropped from the loaded map so a failing disposer cannot wedge a later reload.

**Host API tiers** (`ctx` + the SDK module):
1. read-only state views: `ctx.state.connection()`, `profile()`, `route()`, `theme()` + `subscribe`;
2. curated actions: `toast`, `navigate(path)` (app paths only), `log(level, text)`,
   `restartGateway()`, `onEvent(type, listener)` (session/gateway events);
3. a JSON-RPC door: `ctx.rpc(method, params)` limited to an allow-list of read methods;
4. the plugin's own namespace: `ctx.rest(path)` → `/api/ext/<id>/<path>`, `ctx.socket(path)` → the
   same namespace over the ticketed WS helper;
5. the UI kit: our primitives re-exported by `@indus/desktop-sdk`.
Scoped helpers: `ctx.setTimeout/setInterval/addEventListener` (cleared on unload), `ctx.storage`
(JSON under `indus.plugin.<id>.`), `ctx.onDispose`.

**Delivery.** (a) Disk plugins: one ESM file `<home>/desktop-plugins/<name>.js` (or `.mjs`), at most
16 MiB, no JSX, hot-reloaded on save (main watches the folder, debounces 250 ms, re-checks and
tells the renderer, which unloads and loads again). (b) Unified packages: an agent plugin folder
with a desktop half, `<home>/plugins/<name>/desktop/plugin.js` — off until its agent half is enabled
(P13 consent, `GET /api/plugins` state `enabled`). (c) Bundled: built with the app (the registry's
`registerBundled`).

**Isolation** (R07.19: plugins are not a sandbox). Per the row, isolation is error isolation:
bounded contributions, rollback of a throwing `register`, a React error boundary per contribution,
listeners called in try/catch, a 10 s load deadline. On top (the lead's wave rule — plugin code
never gets the bridge's privileged channels, no Node, CSP-confined):
- main checks every disk plugin before the renderer may load it: size ≤ 16 MiB, UTF-8, the import
  allow-list (only `@indus/desktop-sdk` and `react`, `react/jsx-runtime`; no relative, URL, `node:`
  or bare package imports, no dynamic `import(` of anything) using a scanner that skips strings,
  comments, template text and regex literals; and a deny-list of names that reach privileged
  surfaces (`indusDesktop`, `require`, `process`, `eval`, `Function(`, `importScripts`,
  `WebAssembly`, `__proto__`) — a hit refuses the file with a reason shown in the inventory;
- the renderer runs plugins in its sandboxed, context-isolated page (no Node);
- modules are served by main through the `indus-plugin:` scheme only after the check passed (the
  served text is the checked text, rewritten so the two allowed specifiers point at host shims);
  the CSP adds `indus-plugin:` to `script-src` and nothing else (no `unsafe-eval`, no `blob:`), so a
  plugin cannot pull a second stage from a URL or from text it builds;
- the SDK the plugin gets contains no bridge object: every door it offers is a curated wrapper that
  the host built from the bridge before any plugin ran.
This is defence in depth, not a capability boundary: install needs the same consent and pinning as
agent plugins (D-16.46).

Inputs → outputs: plugin files → `indus:plugins:list` answer `{plugins: [{id, kind, file, size,
hash, status, reason?}]}` and `indus:plugins:changed` events; `indus-plugin://disk/<hash>.js` →
checked module text; `register(ctx)` → contributions in the registry.

Files: `apps/desktop/src/contrib/registry.ts`, `registry.test.ts`, `sdk.ts`, `loader.ts`,
`loader.test.ts`, `host.ts` (the app's `PluginHost` + start-up), `react.tsx` (+ test: render an
area with a boundary per contribution), `testHost.ts`; `apps/desktop/electron/plugins/
diskPlugins.ts`, `diskPlugins.test.ts`, `importScan.ts`, `sdkNames.ts`,
`pluginScheme.electron.test.ts` (real window). Shared: `electron/ipc/channels.ts` + `schemas.ts`
(3 channels: `indus:plugins:list|rescan` invoke, `indus:plugins:changed` event), `src/global.d.ts`
(regenerated), `electron/security/csp.ts` (`indus-plugin:` in `script-src`, the scheme's
privileges) + `csp.test.ts` + `scripts/viteConfig.test.ts`, `electron/main.ts` (scheme
registration + one install line), `src/main.tsx` (start plugins in shell windows),
`src/app/StatusBar.tsx` (the `status-bar` area), `src/profiles/storageFamilies.ts` (the decisions key).

Edge cases / failure modes:
- a throwing `register` → no partial contributions, status `error`;
- `import fs from "fs"` / `node:fs` / `./x.js` / `https://…` / `import("x")` → refused at load;
- `16 MiB + 1` byte file → refused (`too-large`) without reading it fully;
- save storms → one reload per debounce window; delete → unload;
- a module whose evaluation never settles → load times out at 10 s, status `error`;
- the same plugin id from two files → the second is refused (`duplicate-id`);
- non-UTF-8 file → refused.

Our design differences / decisions:
- Served through a main-side scheme after a main-side check (the reference evaluates blob URLs in the
  renderer); CSP stays without `blob:` in `script-src`.
- Deny-list of privileged names at load (beyond the reference's import allow-list).
- Our SDK specifier `@indus/desktop-sdk`, storage prefix `indus.plugin.<id>.`.

Tests we will write:
- registry: throwing register leaves nothing; cap; namespacing + source; commit is one change;
  unload disposers isolated; areas listing and subscribe.
- diskPlugins: `fs` import refused at load; `node:`/relative/URL/dynamic refused; text inside
  strings/comments/regex ignored; 16 MiB + 1 refused, exactly 16 MiB accepted; save hot-reloads
  within one debounce (fake timers); delete unloads; scheme serves only checked text.
- loader: load → register → contributions; reload replaces; timeout; disabled plugins stay
  inventoried but unregistered.

## Decisions for the lead
- The scheme needs `corsEnabled` (module scripts are fetched in CORS mode; found with a real
  window). CSP gains `indus-plugin:` in `script-src` only; `blob:` stays out (the real-window test
  shows a blob module and a remote URL blocked).
- JSON-RPC door limited to read methods (`PLUGIN_RPC_METHODS`); the reference's door is the full
  gateway. Plugin sockets from the desktop are a no-op until main has a ticketed `/api/ext/<id>/`
  route (follow-up).
- Shell areas wired so far: `status-bar` (StatusBar). The other 13 areas have registry support,
  validation and `useContributions`/`ContributionArea`, but their shell consumers (panes into
  `paneRegistry`, routes, sidebar, palette, keybinds, themes, composer, decorations, settings
  pages, transcript directives) are follow-ups; a Settings → Plugins inventory page over
  `PluginLoader.inventory()` is a follow-up too.
- Disk plugin path `<home>/desktop-plugins/<name>.js|.mjs` (one file per plugin, as the row says);
  the reference's `<name>/plugin.js` folder form is not taken.
- `indus-plugin:` scheme + CSP entry (a CSP change, one source added to `script-src`).
- The bridge stays exposed to the page (P20-05); plugins are refused at load when their text names
  it. A real boundary (worker/iframe realm) is post-1.0.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] contribution areas list matches §3.22 (test: `src/contrib/registry.test.ts::covers every area of the row`)
- [x] register rollback; source + id namespacing; cap (tests: `::a throwing register leaves no partial contributions`, `loader.test.ts::a throwing register…`, `::bounds the contributions of one source`)
- [x] import allow-list scanner skips strings/comments/templates/regex; fs/node:/relative/URL/dynamic refused (tests: `electron/plugins/diskPlugins.test.ts::load-time check`)
- [x] 16 MiB + 1 refused without reading; exactly 16 MiB read (test: `::size limit`)
- [x] save hot-reloads within one debounce; a burst is one rescan; delete unloads (test: `::hot reload`)
- [x] unload order: drop from the loaded map first, each disposer isolated (tests: `registry.test.ts::ending the lifetime…`, `loader.ts::unload`)
- [x] load deadline 10 s (test: `loader.test.ts::refused files, wrong ids, bad shapes and slow modules…`)
- [x] inventory: kinds, statuses, package halves off until the agent half is on, live toggle (tests: `loader.test.ts`)
- [x] real window: checked plugin loads through the shims under the CSP; remote + blob blocked (test: `pluginScheme.electron.test.ts`)
- Deliberate differences: served through a main-side scheme after a main-side check (the reference
  evaluates blob URLs); deny-list of privileged names; read-only RPC door; one file per disk plugin.
- Not taken: OS door (pickers, reveal, clipboard), pet door, per-plugin i18n, runAction list — each
  reaches privileged bridge groups and needs its owner's curated wrapper first.

## Status

Partly done (2026-10-21): SDK, registry, disk plugins (check, serve, hot reload), loader, host,
status-bar area. Remaining shell areas and the inventory page are follow-ups (above).

## Wiring (wave 42, WC, D-16.799)

Read: none (wiring of our own modules; no reference file opened).

Done: the shell now reads four more areas. `src/contrib/shell.tsx`: `route` (the route view renders
the contribution matching the path, `classifyPath(path, contributedPaths)` → `contributed`; a
route never shadows a built-in page or overlay; overlays opened over a contributed route return to
it), `sidebar-nav` (`ContributedNav` under the built-in pages), `pane` (`mirrorPaneContributions`
keeps `paneRegistry` in step, so a plugin pane is in the layout toolbar's Open list and leaves with
its plugin). Settings › Advanced › Plugins (`src/contrib/PluginsPage.tsx`): the inventory over
`PluginLoader.inventory()` with status, reason, an on/off switch (`setEnabled`, kept across
starts), "Check the folder again" (`plugins.rescan` → `sync`) and each loaded plugin's
`plugin-settings-page` contributions. `react.tsx` gained `ContributionRegistryProvider` (tests,
previews) and exports `ContributionBoundary`; `loader.ts` remembers the last file list.
Test: `src/contrib/shell.test.tsx::a disk plugin's page, sidebar entry, pane and settings show, and
leave live when turned off` (a real `PluginLoader`, the real `AppRoutes`/`Shell`), plus shadowing,
overlay return and a throwing page; `src/contrib/PluginsPage.test.tsx`.

Decisions for the lead (wiring):
- A plugin pane's `render` gets `{sessionId, profile, active, hidden}` only — not the working
  panes' `connection` (that object reaches every gateway method, past `PLUGIN_RPC_METHODS`).
- A contributed route is matched exactly (no parameters); a deep link to a plugin route opened
  before the plugin loaded falls back like any unknown path (nested → new chat).
- `CONTRIBUTED_PAGES` (`routes.ts`): reserved names served by a bundled contribution — `bots`. While
  that contribution is off, `/bots` is no page (back to a new chat) and has no sidebar entry.

Still follow-ups: consumers of `title-bar`, `palette-command`, `keybind`, `theme`,
`composer-extension`, `session-row-decoration`, `model-menu-decoration`, `appearance-setting`,
`transcript-directive`; plugin sockets (`/api/ext/<id>/` ticketed route).

Status (wave 42): routes, sidebar nav, panes, plugin settings pages and the inventory page wired.
