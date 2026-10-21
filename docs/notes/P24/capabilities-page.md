# Task: Capabilities page (P24-26) Phase: 24 Owner: wave-39 builder U1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/src/app/capabilities/`
(`index.tsx` 215 lines for the tab structure; folder list of `skills/`, `toolsets/`, `connectors/`,
`plugins/`, `mcp/`). Research 08 §3.18. Closed before writing.
Evidence: none.

What it does (own words): one desktop page (the reserved `capabilities` route, P20-15) where the
person sees and changes what the agent can do, in four tabs:
- **Skills**: installed skills with an on/off switch, source (bundled / hub / local) as provenance,
  a hub search with install (behind a confirmation), and uninstall ("archive") for hub skills. An
  install deep link (`indus://skill/...`, P20-34) opens this tab with a confirmation; nothing is
  installed until the person confirms.
- **Toolsets**: each toolset with its tools.
- **Connectors**: MCP servers (local commands and remote URLs): an add form, a Test button, an
  on/off switch, a Sign in button for OAuth servers (the daemon runs the loopback callback; the
  browser opens through `external.open`), and remove.
- **Plugins**: one row per plugin name with a Desktop column (app-level desktop plugins, U3's
  P24-32) and an Agent column (the profile's agent plugins), badged agent / desktop / both; install
  from git with a 40-character commit pin; uninstall.
A scope selector picks the profile the profile-scoped tabs read and write (Skills, Toolsets,
Connectors and the Agent column); switching it reloads only those — the Desktop column is
app-level and stays as it is. Everything goes through the daemon's existing REST routes (P18-14:
`/api/skills*`, `/api/tools/toolsets`, `/api/mcp/servers*`, `/api/plugins*`) by main's proxy
(`api.request`, P20-14) with the chosen profile. The one new backend piece is a
`capabilities.summary` read method that counts each kind for the tab pills.

Inputs → outputs: tab + scope + actions → REST calls (`profile` set for scoped tabs) → lists,
toggles, confirmations, error lines.

Files: `apps/desktop/src/app/capabilities/CapabilitiesPage.tsx`, `tabs.tsx`,
`CapabilitiesPage.test.tsx`, `strings.en.ts` (new), `src/indus/service/capabilities_rpc.py`,
`tests/unit/service/test_capabilities_rpc.py`. Stand-in: `apps/desktop/src/test/_stubs_u1.ts`
(desktop plugin list, U3). Shared: `src/app/App.tsx` (page route), `src/i18n/en.ts`.

Edge cases / failure modes:
- REST failure → the tab shows the message and a Retry; other tabs keep working;
- a deep-link install for a skill already installed → the confirmation says so; Cancel calls
  nothing; confirming twice sends once;
- plugin install without a full 40-hex SHA → refused in the form (the backend requires a pin);
- MCP add with a secret-looking value → the backend's refusal shown as is;
- scope switch while a request is in flight → the late answer is dropped (keyed by scope);
- the plugin uninstall route is not in the backend yet → the row shows "not available on this
  backend" (follow-up).

Our design differences / decisions:
- Built on the existing REST routes rather than new WebSocket methods; `capabilities.summary`
  is an RPC row beside its handler (wiring adds it to the catalogue), and the page falls back to
  counting the lists it loaded when the method is not served.
- The Desktop column comes from a `DesktopPluginSource` interface U3 implements.

Interface assumed (stand-in until integration): `DesktopPluginSource { list(): Promise<{id, name,
version?, enabled}[]> }` (U3, P24-32); `DELETE /api/plugins/{name}` for uninstall (not in P18-14).

Tests we will write:
- Testing Library: scope switch reloads profile-scoped tabs only (the desktop source is not asked
  again, REST calls carry the new profile); install deep link shows a confirmation before any call;
  plugin row badges agent / desktop / both from fixture data; toggles call the right route.
- Python: `capabilities.summary` counts over a temp home (skills enabled/disabled, MCP servers,
  plugins, toolsets).

## Decisions for the lead
- Plugin uninstall needs a REST route (`DELETE /api/plugins/{name}`), not in P18-14.
- The unified-package copy of a plugin's desktop half belongs to U3's SDK; the page only shows the
  columns.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] four tabs skills / toolsets / connectors / plugins (test: `CapabilitiesPage.test.tsx`)
- [x] scope selector shown only when there is more than one profile; profile-scoped tabs keyed by scope; the desktop column read once (test: `::a scope switch reloads the profile-scoped tabs only`)
- [x] counts for the tab pills (test: `::toggles and counts`; `tests/unit/service/test_capabilities_rpc.py`)
- [x] install deep link confirmed before any call; Cancel calls nothing; a double click installs once (tests: `::an install deep link shows the confirmation before any call`, `::confirming installs once into the chosen profile`)
- [x] plugins one row per name with Agent / Desktop columns and badges; git install needs the 40-character pin (tests: `::plugin rows badge agent, desktop and both from fixture data`, `::a plugin install needs a full commit pin`)
- Deliberate differences: REST routes instead of WebSocket methods; a search box instead of an embedded hub page.
- Not taken: official optional-skill catalogue, embedded hub picker, hosted connector accounts, MCP logs and editor, per-tool enable inside a connector (the REST layer has no per-tool route yet), the refresh hotkey, Bot Mode's pinned-profile embedding.

## Status

Done (2026-10-21) with two backend gaps recorded: plugin uninstall (`DELETE /api/plugins/{name}`) and per-tool MCP enable have no REST route. Wiring owed: `CAPABILITY_METHODS` into the catalogue + `register(table)`; pass `summary` (a typed `capabilities.summary` call) and U3's `DesktopPluginSource` to `CapabilitiesRoute` in `App.tsx`; remove `src/test/_stubs_u1.ts`. Shared: `App.tsx` (the route renders the page), `en.ts`.

## Integration (P24 integrator, 2026-10-21)

The page is a workspace page now (`managementSlots` → `capabilities`): its pills read
`capabilities.summary` over the workspace's connection (declared in `indus.protocol.capabilities`,
served by the daemon and the TUI bridge), and the Desktop column reads the running plugin loader's
inventory (`contrib/host.ts::desktopPluginSource`, a plugin is on when it loaded). The stand-in is
gone; the page test keeps its own small source double. Another profile picked on the page shows no
counts (no connection for it in this workspace).
