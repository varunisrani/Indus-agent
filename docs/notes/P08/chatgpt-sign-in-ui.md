# Task: ChatGPT sign-in in the web dashboard and the desktop app (wave 61 M2, D-16.1282) Phase: 08 Owner: builder M2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `web/src/components/OAuthLoginModal.tsx`
(layout of the device-code card only: code box with a copy button, link to the verification page,
waiting line, success line, error box), `apps/desktop/src/components/onboarding/providers.tsx` (layout
of the provider rows and the "connected" tag only), `apps/desktop/src/components/onboarding/flow.tsx`
(searched for the device-code step's layout). No code, CSS or text taken.
Evidence: none (no live sign-in; the backend is M1's, coded against its "Contract for UI" in
`docs/notes/P08/chatgpt-sign-in.md`).

What it does (own words): a person signs Indus in with their ChatGPT plan from the dashboard or the
desktop app, without a terminal. Both use the device flow of M1's REST contract: Sign in asks the
backend for a code, the code is shown large with a copy button and a link to OpenAI's verification
page (new tab / the system browser), and the sign-in session is polled at the interval the backend
gave (never faster) until it is approved, denied, expired, failed or cancelled; each end is said in
plain words, and Cancel ends the session on the backend. Once signed in, the account is listed
(label, plan, when the access expires) with Sign out, which asks first and then deletes the
sign-in on the backend. No token, refresh token or device code ever reaches the browser or the
renderer: the UI only sees the user code, the link, statuses and account metadata.

Inputs → outputs:
- Web Keys › Provider logins (OAuth), the `openai-codex` row (any device-flow row): Sign in →
  `POST /api/providers/login/start` → code + link + status; `GET /api/providers/login/{id}` every
  `interval` s; Cancel → `DELETE /api/providers/login/{id}`; approved → the row reloads
  `GET /api/providers/login/accounts/{provider}` and `/api/model/options`; Sign out → confirm dialog
  → `DELETE /api/providers/login/accounts/{provider}`.
- Desktop onboarding, "Other providers": a "Sign in with ChatGPT" row (provider `openai-codex`) runs
  the same device flow inside the onboarding card, then the model step.
- Desktop Settings › Providers › Accounts: a ChatGPT card with the account (label, plan, expiry),
  Sign in (device flow inside the card) and Sign out (asks first). Everything goes through main's
  existing REST proxy (`desktopBridge().api`); no new IPC channel.
- Model pickers: `openai-codex` is listed like any provider from `/api/model/options` (web) and the
  daemon's `model.options` (desktop) once `configured`; no picker change needed.

Files: `apps/web/src/lib/{restTypes,api}.ts`, `apps/web/src/pages/{ModelsPage,EnvPage}.tsx`, new
`apps/web/src/pages/SignInAccount.tsx`, `apps/web/src/testing/mockApi.ts`,
`apps/web/src/test/routes.generated.ts` (the two rows M1's generator writes), web tests;
`apps/desktop/src/onboarding/{flow.ts,Onboarding.tsx,strings.en.ts,onboarding.css}`,
`apps/desktop/src/settings/pagesB/providers.tsx`, new `apps/desktop/src/settings/pagesB/chatgptAccount.tsx`,
`apps/desktop/src/settings/stringsB.en.ts`, `apps/desktop/src/settings/pagesB/pagesB.css`, desktop
tests and one Electron end-to-end case.

Edge cases / failure modes:
- start refused: 400 `no_device_flow`, 404 `unknown_provider`, 502 `sign_in_unavailable` → the
  backend's message is shown; nothing is polled.
- a poll that fails on the wire is not an answer (web keeps asking at the interval; desktop
  onboarding ends in its error step as before); `interval` 0 or missing → floor of 1 s (web) / 5 s
  default, 1 s floor (desktop).
- the card or overlay closes while pending → polling stops and the session is cancelled
  (desktop; web cancels on Cancel).
- sign-out refused with 409 `inherited_pool` → the message ("belongs to the default profile") shown,
  nothing removed.
- an account with no expiry or no plan → those parts are left out; an expired one says "expired".

Our design differences / decisions:
- The flow is inline (in the row / the card), not a modal: the Keys row already hosted our device
  sign-in panel; the reference uses a modal.
- No browser (PKCE) flow in the UI: OpenAI only redirects to localhost:1455 on the machine that runs
  `indus` (M1's contract); the terminal command stays on the row.
- Copy is ours and never speaks as OpenAI ("Sign in with ChatGPT", "your ChatGPT plan").

Tests we will write: web: the openai-codex row (start → code + copy + link noopener, polls at
`interval`, approved → account with plan and expiry, denied/expired text, Cancel, sign-out with
confirm, 409 refused), mock routes; desktop: onboarding row under "Other providers" and its flow,
the Accounts card (signed out → sign in → signed in → sign out with confirm, failure text), flow
reducer; Electron: the onboarding sign-in through the real REST bridge against the fake backend.

## Decisions for the lead
- (W61-M2-1) The desktop onboarding lists `openai-codex` as "Sign in with ChatGPT" under "Other
  providers" (the brief) and not among the always-visible device rows; other device providers keep
  their place. Change: drop the slug from `SIGN_IN_ROWS` in `onboarding/flow.ts`.
- (W61-M2-2) The web account view and Sign out apply to every device-flow row (the contract's
  accounts routes are per provider); today only `openai-codex` has a device flow.

Compare checklist (filled after the rewrite; reopened `web/src/components/OAuthLoginModal.tsx` and
`apps/desktop/src/components/onboarding/providers.tsx`):
- [x] code large in a framed box with the copy button beside it (web
  `apps/web/src/pages/SignInAccount.test.tsx::starts at once, shows the code with copy and link, …`;
  desktop `apps/desktop/src/settings/pagesB/chatgptAccount.test.tsx::signs in inside the card: …`,
  onboarding `Onboarding.test.tsx::runs the device flow in the card: …`)
- [x] link to the verification page opens outside (web `target=_blank rel=noopener noreferrer`;
  desktop through the bridge's external open; same tests)
- [x] waiting line with a spinner while pending; success line; failure line with Try again / Close
  (web `::says %s plainly and offers Try again`; desktop `::a %s sign-in is said plainly, with Try
  again`, onboarding `::a %s sign-in says so and offers Try again`)
- [x] polls at the backend's interval, never under 1 s; stops on any end (web `::never polls faster
  than once a second, …`; desktop timers pinned to `interval * 1000`)
- [x] Cancel ends the session on the backend (web `::Cancel ends the session …`; desktop
  `::Cancel ends the session on the backend and stops polling`)
- [x] a connected tag on a signed-in provider row (onboarding `::shows Connected on the row once
  signed in`)
- [x] account with plan and expiry, Sign out asks first, 409 `inherited_pool` shown (web `::lists a
  signed-in account and signs out after asking`, `::keeps a sign-in that belongs to the default
  profile …`; desktop `::lists the signed-in account and signs out only after asking`, `::says when
  the sign-in belongs to the default profile`)
- [x] the whole onboarding sign-in over main's real REST proxy against the fake backend (Electron:
  `e2e/core/surfaces.electron.test.ts::first-run onboarding: Sign in with ChatGPT …`)
- Deliberate differences: inline (row / card) instead of a modal; no countdown of the code's
  lifetime (the backend answers `expired`); a failed poll on the wire is not an answer (web keeps
  asking; the desktop card gives up after 10 in a row).
- Not taken: the reference's other sign-in providers, its free-tier rows, its PKCE paste-the-code
  flow and its external-CLI flow.

## Status

Done (2026-10-25), uncommitted in the w61m2 worktree. Shared files outside the new ones:
`apps/web/src/pages/ModelsPage.tsx` (`DeviceLogin`: `autoStart`, code box with copy, status tones,
Try again, `onApproved`; the Models page keeps "Get a sign-in code"), `apps/web/src/pages/EnvPage.tsx`
(OAuth row: "Sign in" replaces "Login", account view), `apps/web/src/pages/EnvPage.test.tsx` (the
"Login" pin now reads "Sign in"), `apps/web/src/testing/mockApi.ts` (contract routes),
`apps/web/src/test/routes.generated.ts` (the two rows M1's generator writes; identical to M1's
hunk), `apps/desktop/src/onboarding/*`, `apps/desktop/src/settings/pagesB/providers.tsx`,
`apps/desktop/src/settings/stringsB.en.ts`, `apps/desktop/src/settings/pagesB/pagesB.css`,
`apps/desktop/e2e/core/surfaces.electron.test.ts`. Needs M1's backend to work for real.
