# Task: Channels, Webhooks and Pairing pages (P18-23) Phase: 18 Owner: builder P1 (wave 32)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `web/src/pages/ChannelsPage.tsx`
(skimmed by search: restart banner, blank-keeps fields, restart outcome watch), `web/src/pages/WebhooksPage.tsx`
(skimmed by search: create form fields, one-time secret block, copy buttons), `web/src/pages/PairingPage.tsx`
(274 lines: pending / approved lists, approve, revoke confirm, clear pending). Our side: the landed routes
`src/indus/web/routes/{messaging,webhooks,daemon,system}.py` (their `ROUTE_LIST` and answer shapes),
`apps/web/src/test/routes.generated.ts`, the notes `rest-channels.md` and `rest-profiles-system.md`.
Evidence: none.

What it does (own words): **Channels** shows one card per messaging platform the gateway knows, with
the state the server works out (switched off, missing credentials, daemon stopped, waiting for a
restart, or what the running daemon reports). Each card has an on/off switch, a form with exactly that
platform's fields — credentials as password inputs that always start empty and show only the masked
preview the server sent, settings and policy values as plain inputs — a per-field "clear" and a Test
button that shows the server's sentence. A save sends only the fields the person typed into (a blank
field keeps what is stored). Any save or switch makes a "restart the daemon" banner appear; it has a
Restart button (the background `daemon-restart` action, whose log is tailed) and it stays until the
daemon itself reports a new process (its pid from `GET /api/daemon` differs from the one seen when
the change was made, or a daemon that was stopped is now running) — never just because the button
was pressed. **Webhooks** lists the receiver's routes (URL, auth, events, delivery, enabled, whether a
secret is set) with the receiver switch, per-route enable switch and delete (confirm; config-file
routes are read-only). Create takes a name, events, prompt, delivery target, deliver-only and auth
mode; the answer's URL and signing secret are shown once with copy buttons and a "you will not see
this again" notice; the secret lives only in that component's state and is gone after navigating
away. **Pairing** lists pending requests (approve) and approved users (revoke, confirm), approves by
the code a person received (platform + code) and clears pending requests (confirm).

Inputs → outputs: `GET/PUT /api/messaging/platforms[/{platform}]`, `POST …/{platform}/test`,
`GET /api/daemon`, `POST /api/daemon/restart`, `GET /api/actions/daemon-restart/status`,
`GET/POST /api/webhooks`, `PUT /api/webhooks/receiver`, `PUT /api/webhooks/{name}/enabled`,
`DELETE /api/webhooks/{name}`, `GET /api/pairing`, `POST /api/pairing/{approve,revoke,clear-pending}`
→ cards, banners, lists, dialogs, toasts.

Files: `apps/web/src/pages/{ChannelsPage,WebhooksPage,PairingPage}.tsx`,
`apps/web/src/pages/{ChannelsPage,WebhooksPage,PairingPage}.test.tsx`, shared helpers
`apps/web/src/ui/ActionLog.tsx` (tails an action), `apps/web/src/ui/CopyButton.tsx`; types in
`apps/web/src/lib/restTypes.ts`, calls in `apps/web/src/lib/api.ts`, mock routes in
`apps/web/src/testing/mockApi.ts`, page rows in `apps/web/src/shell/routes.ts`.

Edge cases / failure modes:
- A credential's masked preview is never put into an input (the server refuses it back with
  `redacted_value`); inputs start empty on every load.
- Unknown field / refused name / bad value (400) → the server's sentence under the form; nothing sent
  is half-applied (the server checks the whole body first).
- Daemon stopped when the change is made → banner says "start" and clears once the daemon runs.
- Restart action fails or the daemon does not take a remote stop → the action log shows the server's
  line; the banner stays.
- Restart already running (409 `action_running`) → toast, the log keeps tailing.
- Webhook name rules / duplicate (400 / 409) shown inline; config routes have no switch or delete.
- Pairing approve: unknown or expired code (404), locked out (429) → plain sentences.
- Banner memory survives a reload of the page (session storage per profile), not other profiles.

Our design differences / decisions:
- Banner clears on the daemon's own report (pid change), not on the button press (the reference hides it
  when the restart call returns).
- No platform setup wizard links or QR pairing flows here (the CLI's `indus gateway setup` owns those).
- Settings/policy fields show their current value (not secret); credentials never.

Tests we will write:
- Credential inputs empty on load with the preview shown; a save sends only typed fields (blank keeps);
  clear sends `clear_env`; enable switch sends `enabled`.
- Restart banner appears after a save, survives the restart click and a status poll with the same pid,
  clears once `/api/daemon` reports a new pid; the restart action log is tailed.
- Test button shows the server's message.
- Webhook create shows URL + secret once with the notice; after navigating away and back the secret is
  gone and no list answer carries it; enable switch, delete confirm; config route read-only.
- Pairing: approve a pending request, approve by code, revoke after confirm, clear pending after confirm,
  error sentences for 404/429.

## Decisions for the lead
- Banner persistence: `sessionStorage["indus.channels.restart.<profile>"] = {pid}`; clears on a new pid
  (or running after stopped). Change in `ChannelsPage.tsx` (`readRestartMark`).
- The secret is kept out of the query cache: the create mutation has `gcTime: 0` and the page copies
  the secret into component state only.

Compare checklist (filled after the rewrite; reopened the three pages by search):
- [x] blank keeps stored values; only typed fields sent (test: `apps/web/src/pages/ChannelsPage.test.tsx::sends only the fields that were typed into`)
- [x] secret inputs empty on load (test: `ChannelsPage.test.tsx::starts every credential input empty and shows only the masked preview`)
- [x] restart banner clears only on a daemon report (test: `ChannelsPage.test.tsx::keeps the restart banner until the daemon reports a new process`)
- [x] secret shown once (test: `apps/web/src/pages/WebhooksPage.test.tsx::shows the signing secret once and not after navigating back`)
- [x] pairing approve / revoke / clear (tests: `apps/web/src/pages/PairingPage.test.tsx`)

- [x] restart action tailed through the background-action status route, not a fire-and-forget call (same test as the banner)
- [x] route shapes are the landed ones (test: `apps/web/src/lib/api.admin.test.ts::only use generated routes, scoped exactly when the route is`)
- Deliberate differences: the reference hides its restart banner when the restart call returns; ours waits for the daemon's own report (new pid). Approve-by-code is ours (the row asks for it; the reference approves listed requests only).
- Not taken: per-platform setup links / QR flows, hot-reload hints (our routes have no `hot_served`), the gateway "migrate" action.

## Status

Done (2026-10-20). Shared edits: `apps/web/src/lib/{api,restTypes}.ts`, `apps/web/src/testing/mockApi.ts`, `apps/web/src/shell/routes.ts`. New shared UI: `apps/web/src/ui/{ActionLog,CopyButton}.tsx`.

Integration (wave 32, P18c integrator, 2026-10-21): decisions numbered D-16.651.
