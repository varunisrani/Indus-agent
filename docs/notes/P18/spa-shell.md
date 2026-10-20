# Task: SPA shell (P18-17) Phase: 18 Owner: builder F1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `web/src/App.tsx` (route table, nav list,
redirects, sidebar collapse key, persistent chat host block, profile-keyed routes), `web/package.json`
(stack versions only). Research spec `docs/plan/research/09-web-acp-mcp-platform.md` §3.2.
Evidence: none (prework against mocks; no capture of the running reference yet).

What it does (own words): `apps/web` is the browser half of `indus dashboard`: a Vite + React 19
single-page app. A left sidebar lists the pages; it collapses to an icon rail (remembered in
`localStorage` under `indus-sidebar-collapsed`) and becomes a slide-over drawer below 1,024 px. A page
header shows the page title and an "end" slot each page fills with its own actions. `/` and any unknown
path land on `/sessions`. Every page is a lazy chunk. The chat terminal is special: it lives outside
the router, is mounted the first time `/chat` is visited and afterwards only hidden (`display:none`,
`aria-hidden`) so the PTY socket and terminal survive tab switches; `/chat` keeps an empty route so deep
links and history work. A profile switcher appears when the machine has more than one profile; a
non-default selection shows an amber banner and remounts the page tree so no page keeps the old
profile's data. A light/dark switcher writes `data-theme` on `<html>` from the `@indus/ui` tokens.
Network failures surface as one plain "backend unreachable" toast; details stay in the console.

Inputs → outputs: window globals injected by the backend (`__INDUS_BASE_PATH__`,
`__INDUS_DASHBOARD_EMBEDDED_CHAT__`, `__INDUS_INITIAL_PROFILE__`, `__INDUS_DASHBOARD_PROFILE__`), the
`/api/profiles` list, `/api/config` flag `dashboard.show_token_analytics` → sidebar, routed page, hidden
chat host, banners, toasts.

Files: `apps/web/{package.json,index.html,vite.config.ts,tsconfig.json,vitest.config.ts}`,
`apps/web/src/{main.tsx,App.tsx,App.test.tsx,index.css}`, `apps/web/src/shell/{routes.ts,Sidebar.tsx,
PageHeader.tsx,ChatHostSlot.tsx,ProfileScope.tsx,Toasts.tsx,ThemeSwitch.tsx}`, `apps/web/src/ui/*`.

Edge cases / failure modes:
- `localStorage` unavailable (private mode) → collapse state just is not remembered.
- Base path served under a prefix → router basename from `__INDUS_BASE_PATH__`, normalised.
- Analytics nav hidden unless the config flag is on, but the route still resolves (deep link).
- `/docs` is an external link (new tab), not a routed page.
- Profile list fails to load → no switcher, pages still work against the backend's own profile.
- Chat disabled by the backend flag → no chat nav item, `/chat` redirects like any unknown path.

Our design differences / decisions:
- Our names: globals `__INDUS_*`, storage keys `indus-sidebar-collapsed`, `indus-theme`.
- No language switcher (D-16.51: English only in 1.0), no dashboard-plugin slots (L17 later).
- Fonts: system stacks from `@indus/ui` only, no font files shipped (OFL rule trivially met, R09.22).
- The chat host component is P18-26's; until it lands the slot renders a small "terminal not built
  yet" placeholder, injected through a prop so the lead swaps one line.
- Status strip (P18-25) and auth widget (P18-28) get named slots in the sidebar footer.

Tests we will write:
- `/` and an unknown path redirect to `/sessions`; every route in the table resolves.
- Navigating `/chat` → `/logs` → `/chat` keeps the same chat host DOM node; hidden off-route.
- Sidebar collapse persisted and restored; slide-over below 1,024 px.
- Profile switcher hidden with one profile, banner for a non-default one.
- axe run on the rendered shell has no violations.

## Decisions for the lead
- Routes table is `apps/web/src/shell/routes.ts`; later page tasks add one row each.
- Chat slot wiring: replace `ChatPending` with `lazy(() => import("../chat/ChatHost.tsx"))` at P18-26.

Compare checklist (filled after the rewrite; reopened `web/src/App.tsx`):
- [x] `/` and unknown → `/sessions` (test: `apps/web/src/App.test.tsx::redirects the root and unknown paths to sessions`)
- [x] chat host mounted on first visit, hidden after (test: `App.test.tsx::keeps the same chat host node across tab switches`)
- [x] collapse key remembered (test: `App.test.tsx::remembers the collapsed sidebar`)
- [x] drawer closes on Escape and on navigation — gap closed (test: `App.test.tsx::becomes a slide-over below 1,024 px`)
- [x] routed pages remount on a profile switch — fixed by keying the routed tree on the profile
  (test: `App.test.tsx::shows the switcher and banner for a non-default profile`)
- Deliberate differences: no plugin slots or plugin routes (L17), no i18n (D-16.51); the unknown-route
  redirect has no plugin-loading wait because no plugins load.
- Not taken: brand fonts, theme gallery beyond our light/dark pair (D-16.51).

## Status

Done (2026-10-20). Shell, routes and slots built; chat host and status strip/auth widget slots wait
for P18-26 / P18-25 / P18-28.
