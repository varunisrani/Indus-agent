# Task: Settings overlay (P20-32) Phase: 20 Owner: wave-35 builder D3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/src/app/settings/index.tsx`
(all: page list, nav groups, sub-page redirect, search pill, footer actions), `constants.ts` (section
list and the curated key lists), `config-subpages.ts` and `other-subpages.ts` (child pages and the
field → page rule), `settings-search.ts` (entries, scoring, target → query), `settings-manifest.ts`
(head: hand-built rows carry their own copy and keywords), `profile-scope.tsx` (the "Applies to"
chips), `gateway-settings.tsx` (skim: connection state, keychain toggle), `model-settings.tsx`
(skim: main model defaults block). Closed before writing.
Evidence: none.

What it does (own words): `/settings` is an overlay over the route it was opened from. A left
navigation lists the views; each view has child pages, and the header shows a breadcrumb
"Settings › View › Page". Views in 1.0: Model, Chat, Appearance, Workspace, Safety, Memory, Advanced,
Providers, Gateways, Keyboard, Notifications, Sessions, About. A search box above the navigation
matches every setting the app knows — schema fields from the backend and the hand-built rows — and a
hit opens the child page that owns it (the field is scrolled to and outlined). Links are query
strings on the overlay route (`/settings?view=safety&page=privacy&field=security.fake_ip_ranges`);
a link naming only a field, or a view without a page, resolves to the owning page, and an unknown
view lands on the first one. When the backend lists two or more profiles, config-backed pages show
an "Applies to" chip row; the chosen profile is passed as `profile` on every REST call of the page,
and the choice resets to the active profile whenever the active profile changes.

Config-backed pages read `GET /api/config/schema` (fields by dotted path: type, description,
options) and `GET /api/config` (the form tree, credentials replaced by the placeholder) and write
one-field patches with `PUT /api/config {config: {a: {b: value}}}` (the writer is additive, so a
patch never drops other settings). Controls by type: boolean → switch, enum → select, string →
text, integer/number → number input (invalid text is not sent), list → one item per line. Secret
settings show the placeholder and a reveal toggle for what the person types; an untouched secret is
never re-sent. Provider API keys and other env rows go through `/api/env` (save / clear; a stored
key shows only its redacted preview — no route returns a value). Device-code accounts use
`/api/providers/login/*`; custom endpoints are `providers.<name>` entries with a Test that calls the
backend's `POST /api/model/test-endpoint` (the endpoint's own `/models` route, from the backend).
The daemon's `config.set` stays for the live session switches the status bar and model picker
already use; it only knows those few keys.

Desktop-held preferences (minimise to tray, keep awake, quick entry shortcut, notifications,
keyboard bindings, chat text scale) live in main as one small preferences file read and written over
`indus:prefs:get|set` with an `indus:prefs:changed` event; the main-process owners of those features
(P20-35, P20-36) read the same object. UI scale is the window zoom (`zoom.get|set`, 100 % default),
chat text scale defaults to 110 %. Gateways uses `connection.*` (mode, saved remote: test / save /
forget) and `secrets.getPolicy|setPolicy` (keychain vs file). About and Advanced › Desktop use
`updates.*` (P20-37) and show "not available in this build" until it lands.

Inputs → outputs: overlay route query → view/page/field; schema + form tree → controls; a change →
`PUT /api/config` patch (with `profile` when a chip is chosen) → the saved value or an inline error.

Files: `apps/desktop/src/settings/{SettingsOverlay.tsx,schemaPage.tsx,pages.ts,search.ts,
settingsApi.ts,context.tsx,customPages.tsx,keybindings.ts,PrefsEffects.tsx,settings.css,strings.en.ts,
fakeRest.ts (test fake)}`, `apps/desktop/src/app/settingsSlots.tsx`, tests `search.test.ts`,
`schemaPage.test.tsx`, `pages.test.ts`, `keybindings.test.ts`, `SettingsOverlay.test.tsx`;
main: `apps/desktop/electron/prefs.ts` (+ `prefs.test.ts`); backend: `POST /api/model/test-endpoint`
in `src/indus/web/routes/models.py` (+ test). Shared: `electron/ipc/{channels,schemas}.ts` (three
prefs rows), `src/global.d.ts` (regenerated), `src/app/App.tsx` (overlay slot), `src/main.tsx`,
`src/i18n/en.ts` (spread), `electron/main.ts` (install line).

Edge cases / failure modes:
- schema or config unreadable (500 `config_unreadable`) → the page shows the backend's message and
  writes nothing; a managed install (403) → controls disabled with the reason;
- a field the schema does not know (old backend) → not shown; a page with no known field → "nothing
  to change here on this backend";
- a link to an unknown view/page/field → first view / first page of the view;
- the active profile changes while a chip is chosen → chip resets, page reloads for the active one;
- one profile only → no chip row, requests carry no `profile`;
- number input "abc" → not sent, the field shows invalid; list with blank lines → blanks dropped;
- a secret placeholder echoed back is never written (only a new value is sent);
- main channels not landed yet (`not-available`) → the row says so instead of failing the page;
- keyboard rebinding to a combination another action uses → conflict shown, not saved until the
  other action is changed or the person confirms the swap.

Our design differences / decisions:
- Search is a box inside the overlay (no command palette in 1.0); "type to search" focuses it.
- Child pages are declared once in `pages.ts` (fields and prefixes per page); search, links and the
  renderer all use the same `pageForField` rule, so a hit can never point at a page that does not
  render the field.
- Config writes go through REST `PUT /api/config` (the daemon's `config.set` only knows the live
  session keys); see "Decisions for the lead".
- Not built: billing / vendor accounts (D-16.12), plugin settings pages, vault, voice, browser,
  local model downloads, export/import/reset of the whole config file (later; the web dashboard has
  config editing).

Tests we will write:
- search: "proxy" lands on Safety › Privacy (the field whose description names a proxy); a hand-built
  row ("tray") lands on its page; ranking (label prefix before description); empty query → nothing;
- links: field-only link resolves view + page; unknown view → first view; page outside the view →
  first page;
- schema page: enum → select, boolean → switch, string → text, number, list; a change sends one patch
  with the profile; placeholder never re-sent; managed 403 disables;
- overlay: chip row only with ≥ 2 profiles; chip resets when the active profile changes; breadcrumb;
- custom endpoint Test calls `POST /api/model/test-endpoint` on the fake and shows the model count;
- keybindings: conflict detection, invalid accelerator refused;
- prefs (main): defaults, invalid file ignored, atomic write, set merges, change event.

## Decisions for the lead
- Config pages write through REST `PUT /api/config` (one-field patches) instead of the daemon's
  `config.set`: our `config.set` accepts only the live session keys (model, yolo, reasoning, …). The
  status bar and model picker keep `config.set` for those.
- New backend route `POST /api/model/test-endpoint {base_url, api_key?, key_env?}` → `{ok, status,
  models, message}` (the endpoint's `/models` route from the backend, 10 s, never raises): the
  "Test" of a custom endpoint must hit the real route from where the agent runs (remote-safe), and
  no route did that.
- New channels `indus:prefs:get|set` (invoke) and `indus:prefs:changed` (event), owner P20-32:
  desktop-held preferences in `<userData>/desktop-prefs.json`; P20-35/36 read them in main from the
  `DesktopPrefs` object (`installPrefsIpc` returns it).
- Env-key "reveal" shows the typed value only; stored values are never sent back (the backend has
  no reveal route by design, R04.15).
- Settings links live in the overlay route's query (`/settings?view=&page=&field=`).

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Page list and child pages; a field's page is the first that claims it (test:
  `src/settings/search.test.ts::a field belongs to the first page that claims it, and only that page renders it`)
- [x] Search → owning child page; saved links resolve (tests: `search.test.ts::"proxy" lands on the
  child page that owns the proxy setting`, `::a link naming only a field lands on the owning child page`,
  `SettingsOverlay.test.tsx::search "proxy" opens the owning child page and outlines the field`)
- [x] Scope chip only with ≥ 2 profiles, scopes every request, resets on active profile change (tests:
  `SettingsOverlay.test.tsx::no chips with one profile; with two the chosen one scopes every request`,
  `::the chip resets when the active profile changes`)
- [x] Schema controls enum/bool/string/number/list; one patch per change; secrets never re-sent; managed
  install disables (tests: `src/settings/schemaPage.test.tsx`)
- [x] Custom endpoint Test calls the backend route (tests: `SettingsOverlay.test.tsx::a custom
  endpoint's Test calls the backend route and shows what it found`,
  `tests/contract/web/test_model_endpoint_route.py`)
- [x] Keys through the backend, cleared from the field (test: `SettingsOverlay.test.tsx::an API key is
  saved through the backend and cleared from the field`)
- [x] Desktop-held rows via prefs; main channels not landed show "not available" (tests:
  `SettingsOverlay.test.tsx::desktop-held rows …`, `::main channels that have not landed …`,
  `electron/prefs.test.ts`)
- [x] Type-to-search (printable keys outside a field go to the search box) — the reference opens its
  command palette; ours fills the inline box.
- Deliberate differences: inline search box instead of a command palette; pages and field ownership in
  one table (`pages.ts`) shared by search, links and rendering; REST `PUT /api/config` patches instead of
  the daemon's `config.set`; a stored env value is never revealed (no route returns one).
- Not taken: export / import / reset-to-defaults of the whole config (the web dashboard has config
  editing), billing (D-16.12), plugin settings, vault, voice, browser, local model downloads (later
  phases), repo-scan settings (P20-25 owns where they live; add a page row when it lands).

## Status

Done (2026-10-21). Shared changes: `electron/ipc/{channels,schemas}.ts` (+ `indus:prefs:get|set|changed`),
`src/global.d.ts` (regenerated), `src/app/App.tsx` (`overlays` and `layers` slots), `src/main.tsx`
(`settingsSlots`), `src/i18n/en.ts` (spread), `electron/main.ts` (`installPrefsIpc`), backend
`src/indus/web/routes/models.py` (`POST /api/model/test-endpoint`) and the regenerated
`apps/web/src/test/routes.generated.ts`. Hand-on: P20-35/36 read `DesktopPrefs` (returned by
`installPrefsIpc`) for tray, keep awake, quick entry and HUD shortcuts; P20-37's `updates.*` light up
the About and Advanced › Desktop rows; P20-26's `profile.onChanged` resets the chips.

Integration (wave 35, D-16.721, D-16.723): new pages Workspace › On this computer (default folder for new chats, repository scan, data-URL read cap) and Advanced › Profiles (rename/delete through the switcher); the logs viewer in About; Gateways save/forget inside `switcher.applyConnectionChange`; the desktop preferences are the one store the ambient step applies (`electron/ambient/prefsBridge.ts`); the tray row stays switchable and says why when the tray is refused.
