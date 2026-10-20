# Task: REST client (P18-18) Phase: 18 Owner: builder F1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `web/src/lib/api.ts` (header through
`buildWsUrl`: base path, session header, profile-scoped family list, 401 handling, ticket fetch; the
`api` object skimmed for paths and the REST interfaces), `web/src/lib/session-mutations.ts`,
`apps/shared/src/session-http-mutations.ts` (identity fields only), `web/src/lib/api-files.ts`.
Research spec 09 §3.3–§3.4. Evidence: none.

What it does (own words): one `request()` function every page uses. It prefixes the base path, adds
`X-Indus-Session-Token` when the backend injected a loopback token, sends cookies for gated mode, and
appends `?profile=<selected>` to URLs in the profile-scoped endpoint families unless the URL already
names a profile; with nothing selected it uses the backend's own profile name so writes on a
multi-profile host never go out unnamed (R09.20). A 401 is handled in one place: a gated envelope
(`unauthenticated`/`session_expired` + `login_url`) stores where the user was in
`sessionStorage["indus.lastLocation"]` and navigates the whole page to the login URL; in loopback mode
a 401 means the token rotated under an open tab, so the page reloads exactly once (a session-storage
guard, cleared by the next 2xx); gated mode never reloads (R09.5). Every failure becomes
`ApiError { status, message, details }` with status 0 for "unreachable". `buildWsUrl` makes the
`ws(s)://` URL with either `token` (loopback) or a fresh single-use `ticket` fetched by POST (8 s
timeout). `testing/mockApi.ts` is a typed in-memory backend behind a `fetch` stand-in for page tests.

Inputs → outputs: path + method + JSON body, window globals → parsed JSON or `ApiError`; side effects:
one reload, a full-page navigation, a toast event on network failure.

Files: `apps/web/src/lib/{api.ts,wsUrl.ts,bootstrap.ts,restTypes.ts,api.test.ts}`,
`apps/web/src/testing/mockApi.ts`, `apps/web/src/test/routes.generated.ts` (generated route list; was the `_stubs_f1.ts` fixture).

Edge cases / failure modes:
- Family match is on path segments: `/api/model` must not capture `/api/models`.
- `profile=` already present (explicit) wins; the scoped list is one exported constant.
- Non-JSON 401 or a domain 401 without the envelope in gated mode → ordinary `ApiError`.
- Reload guard storage unavailable → treat as already tried (never loop).
- Ticket POST hangs → aborted at 8,000 ms → `ApiError(0)`.
- 204 / empty body → `undefined`.

Our design differences / decisions:
- Header `X-Indus-Session-Token`, storage keys `indus.lastLocation`, `indus.tokenReload`.
- Family list (our paths): status, daemon, analytics, skills, tools/toolsets, config, env, mcp,
  messaging, providers/login, model, pairing, memory, webhooks, ops, logs, credentials, plugins.
- Navigation and reload go through a small replaceable `browser` seam (tests, and jsdom cannot
  navigate).
- REST types are hand-written in `lib/restTypes.ts` until P18-11..16 generate them into
  `@indus/protocol`; then that file re-exports the generated names.

Tests we will write:
- Loopback 401 reloads once; the second 401 surfaces an error; a 2xx re-arms the guard.
- Gated 401 stores the location and navigates; never reloads in gated mode.
- Scoped family coverage against the route list; explicit `profile=` wins; fallback profile.
- Header present in loopback, absent in gated; base path prefix; network failure → status 0 + event.
- Ticket fetch timeout at 8 s with fake timers; WS URL picks token vs ticket.

## Decisions for the lead
- The backend route modules (P18-11..16) should use exactly the paths in `api.ts` (listed in the
  report); the stand-in route list in `src/test/_stubs_f1.ts` must be replaced by the generated one.

Compare checklist (filled after the rewrite; reopened `web/src/lib/api.ts` header):
- [x] reload once, guard cleared on 2xx (test: `apps/web/src/lib/api.test.ts::reloads once on a loopback 401, then surfaces the error`)
- [x] gated navigation with stored location (test: `api.test.ts::navigates to the login URL on a gated 401`)
- [x] explicit profile wins (test: `api.test.ts::leaves an explicit profile alone`)
- [x] raw fetch for uploads uses the same scope and auth — `rawRequest` shares the header/scope code
  (test: `api.test.ts::raw requests carry the token and the profile`)
- [x] a caller can keep a 401 local (the auth widget's identity probe) — gap closed with `allowUnauthorized` (test: `api.test.ts::lets a caller keep a 401 to itself`)
- [x] prefix matching on segments, not raw `startsWith` — gap closed in our version
  (test: `api.test.ts::scopes by whole path segments`)
- Deliberate differences: family names follow our route names (daemon not gateway; no portal,
  curator, shared-metrics, local-models, dashboard theme/font/plugin families: dropped or later).
- Not taken: plugin-socket profile injection in `buildWsUrl` (dashboard plugins are later, L17).

Integration (P18 integrator, 2026-10-20): `lib/wsUrl.ts` no longer puts the credential in the
query: `wsTarget(path, purpose, params)` returns the credential-free URL and the subprotocols
(`indus-dashboard-v1`, `indus-dashboard-token.<t>` or `indus-dashboard-ticket.<t>`), and
`openDashboardSocket` dials with them (D-16.580). `src/test/_stubs_f1.ts` stays as a fixture
(first line renamed) until P18-11..16 generate the route list.

## Status

Done (2026-10-20). Generated route list and REST models owed by P18-11..16.

Integration (P18 REST integrator, 2026-10-20): `src/test/_stubs_f1.ts` is deleted; `api.test.ts`
reads `src/test/routes.generated.ts`, written and compared by `tests/contract/web/
test_route_list.py` from the route modules' `ROUTE_LIST` constants (D-16.598). Against the real
list the client gained `/api/actions` and `/api/system` as scoped families and `UNSCOPED_PATHS`
(config schema / defaults, cron fire, MCP OAuth callback); `fileDownloadUrl` now mints a download
ticket (no `?token=`, D-16.596); `StatusResponse` / `api.status()` and a nullable hub verdict were
added to `restTypes.ts`.
