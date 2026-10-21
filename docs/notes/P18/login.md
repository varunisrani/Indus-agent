# Task: login page and auth widget (P18-28) Phase: 18 Owner: builder P3 (wave 32)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/dashboard_auth/login_page.py`
(flow only: which providers render what, how `next` and the proxy prefix are threaded, where the
password form posts; markup and styles not used), `web/src/components/AuthWidget.tsx` (states and
the identity call), `web/src/components/AuthWidget.test.tsx` (test names only),
`web/src/components/auth-widget-visibility.ts`, `web/src/lib/api.ts` (`getAuthMe`, `logout` only),
`web/src/components/SidebarFooter.tsx` (where the widget sits). Research spec
`docs/plan/research/09-web-acp-mcp-platform.md` §3.3.
Evidence: none (no capture of the running reference).

What it does (own words): in gated mode (a bind beyond loopback, or a public URL) a person who is
not signed in lands on `/login`. The server renders that page itself: no React, no bundle, no
script at all, so nothing secret can be injected into it and a strict Content-Security-Policy holds
(`default-src 'none'`, the one `<style>` allowed by its SHA-256 hash, forms only to ourselves, no
framing, no base URL). It lists every configured interactive provider in registry order: a
username/password provider becomes a plain HTML form posting to `/auth/password-login` (the server
answers with a 303 to the landing page, or back to `/login?error=…`), a redirect (OIDC) provider
becomes a link to `/auth/login?provider=<name>&next=<path>` which starts the PKCE round trip. The
landing path (`next`) and the reverse-proxy prefix are threaded into every form and link. Errors are
chosen from a fixed list of codes (`1`/`invalid` for wrong credentials, `rate_limited`,
`unavailable`), never echoed from the URL. Loopback mode has no sign-in: `/login` redirects to the app; the headless
backend answers JSON 404.

Inside the SPA the auth widget sits in the sidebar footer (`App`'s `sidebarFooter` seam). It asks
"who am I" (`GET /api/auth/me`) with `allowUnauthorized`, so a stale loopback token does not trigger
the global reload-once path. Loopback identity (`provider: "loopback"`), a 401/403 or loopback mode →
the widget renders nothing. A gated identity shows the display name when there is one, the user id
cut to 14 characters (full id in the tooltip), "via <provider display name>" (looked up in
`GET /api/auth/providers`, falling back to the provider key) and a sign-out button. Sign-out POSTs
`/auth/logout` (same-origin, so the gate's Origin rule holds), forgets the remembered location and
navigates the whole page to `<prefix>/login`. A network failure shows "Sign-in status unavailable"
(or "Could not sign out") instead. After a sign-in, the first gated identity answer consumes
`indus.lastLocation` (written by the REST client when a gated 401 sent the browser to the login
page) and routes back there once.

Inputs → outputs: provider registry + `next`/`error` query + proxy prefix → login HTML with strict
headers; `/api/auth/me` + `/api/auth/providers` → widget state; logout click → POST + navigation.

Files: `src/indus/web/auth/login_page.py` (new: `render_login_page`, `login_headers`,
`LOGIN_ERRORS`), `src/indus/web/routes/auth.py` (shared: `render_login` replaced by the new module,
form-post failures redirect with an error code), `apps/web/src/shell/AuthWidget.tsx` (new),
`apps/web/src/lib/auth.ts` (new: identity, providers, sign-out and last-location helpers),
`apps/web/src/main.tsx` (shared: widget passed into `sidebarFooter`), `apps/web/src/lib/api.ts`
(shared: one exported `assignLocation` over the existing navigation seam), tests
`tests/unit/web/auth/test_login_page.py`, `apps/web/src/shell/AuthWidget.test.tsx`; ledger line in
`docs/internals/traps-ledger.md` (R09.23).

Edge cases / failure modes:
- no interactive provider → the page says none is configured (a gated app cannot start without one,
  but the renderer stays total).
- a provider name or display name with HTML in it, a `next` with quotes or `<` → escaped; a `next`
  that is not a same-origin path (`//evil`, `https://…`, backslashes) is dropped by `safe_next`.
- `error=<anything else>` → no message (nothing from the URL reaches the page).
- proxy prefix → every action and href carries it.
- the CSP hash must change when the stylesheet changes: computed from the very text at import.
- rate limit and provider outage on a form post → 303 back to the page with `rate_limited` /
  `unavailable` (JSON clients keep the 429 / 503 bodies).
- widget: request in flight while unmounting → no state update; identity 5xx → "unavailable";
  providers call failing → provider key shown.
- `indus.lastLocation` pointing outside the base path or at `/login` → ignored and cleared.

Our design differences / decisions:
- No script on the login page at all (the reference wires its password form with an inline
  script and `fetch`); our form is a plain POST and the server answers with redirects. This keeps
  `script-src` absent from the CSP.
- The style is allowed by hash, not `'unsafe-inline'`; no web fonts (system stacks; R09.22) so no
  `font-src`; our own copy and our light/dark colours (the `@indus/ui` token hex values).
- Extra headers on the page: `X-Content-Type-Options: nosniff`, `Cross-Origin-Opener-Policy:
  same-origin`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.
- The widget shows the user id (row) with the display name above it when present, not "display
  name or email or id".
- No native-app provider choice page (D-16.589).

Tests we will write:
- Python: page lists a password form and an OIDC link (both providers at once), each with `next`
  and the proxy prefix; the link starts the PKCE round trip; CSP present, hash matches the inline
  style, no `<script`, no token or secret on the page; escaping; only known error codes shown;
  unsafe `next` dropped; loopback redirect; serve 404; form post rate-limited → 303 with
  `rate_limited`; form sign-in then logout clears every auth cookie (TestClient); a cross-origin
  logout POST is refused 403 (P18-03's Origin rule).
- TS: loopback identity renders nothing; 401 renders nothing and never reloads; gated identity
  shows the truncated id (14 chars + ellipsis), the provider display name; logout POSTs
  `/auth/logout` under the base path, clears `indus.lastLocation` and navigates to the login page;
  network failure message; stored last location restored once after sign-in.

## Decisions for the lead
- Form-post failures redirect to `/login?error=<code>` for rate limiting and provider outage too
  (they answered JSON to a browser form before). JSON clients are unchanged.
- `assignLocation(url)` exported from `lib/api.ts` so the widget navigates through the same test
  seam the 401 flow uses (one line in a shared file).
- The widget is passed into `sidebarFooter` from `main.tsx`; P18-25's status strip joins it there.

Compare checklist (filled after the rewrite; reopened `hermes_cli/dashboard_auth/login_page.py`
(`render_login_html`, `_apply_proxy_prefix`, `_render_password_form`),
`web/src/components/AuthWidget.tsx`, `web/src/lib/api.ts` (`getAuthMe`, `logout`)):
- [x] Every interactive provider rendered: password form, redirect link with `provider` and `next`
  (test: `tests/unit/web/auth/test_login_page.py::test_both_providers_render_with_next_and_prefix`)
- [x] Proxy prefix threaded into form action and link (same test)
- [x] Page carries no token or injected bootstrap (test: `::test_page_has_no_script_and_no_secret`)
- [x] Values escaped (test: `::test_values_are_escaped`)
- [x] Username field: no autocapitalise / autocorrect / spellcheck — gap found (autocorrect) and
  added in `_password_form`
- [x] Widget hidden for the loopback identity and on 401 (test: `AuthWidget.test.tsx::renders
  nothing for the loopback identity`, `::keeps a 401 to itself: nothing shown and no reload`)
- [x] User id cut at 14 characters with an ellipsis (test: `AuthWidget.test.tsx::shows the user id
  cut to 14 characters and the provider's name`)
- [x] Logout POSTs `/auth/logout` then navigates to `/login` (test: `AuthWidget.test.tsx::signs
  out: POST under the base path, forgets the last location, opens the login page`)
- [x] Network failure of the identity call → a short "unavailable" line (test: `::reports an
  identity it cannot read`)
- Deliberate differences: the reference's password form is driven by an inline script calling
  `fetch` and shows the error in place; ours is a script-free form whose errors come back as fixed
  codes in a redirect, so the CSP has no `script-src`. The reference's widget labels display name →
  email → id; ours always shows the id (the row) with the display name above it.
- Not taken: the native-app provider choice page (D-16.589); brand fonts and backdrop.

## Status

Done (2026-10-20). Shared edits: `src/indus/web/routes/auth.py`, `apps/web/src/main.tsx`,
`apps/web/src/lib/api.ts` (one export), `docs/internals/traps-ledger.md` (one R09.23 line).

Integration (wave 32, P18c integrator, 2026-10-21): D-16.656. The browser run (P18-30) found that no sign-in could succeed in a real browser: under `Referrer-Policy: no-referrer` Chromium sends `Origin: null` on the form's POST and the gate refuses it as cross-site. The page now asks for `same-origin` (header and meta); pinned by `test_the_form_post_keeps_its_origin` and `apps/web/e2e/auth.spec.ts`.
