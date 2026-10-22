# Task: Security regression suite, desktop and web (P25-03) Phase: P25 Owner: SEC2 (wave 43)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/window-open-policy.ts`
(whole file: the deny-always handler and its one delegated exception), `apps/desktop/electron/hardening.ts`
(the list of guarded operations only: secret-file modes, safe-storage use, sensitive-file and path refusals,
fetch timeouts). Closed before writing.
Evidence: none.

What it does (own words): one place that proves each desktop and web trust boundary still holds, so a
later change that weakens one fails a named test. The desktop half has two layers. A unit audit runs in
the root vitest lane on every machine: every channel in the IPC table, sent from each kind of foreign
frame (a `<webview>` guest, an iframe, a page that navigated away from the app, a window main never
registered, a destroyed sender, an event with no frame), is refused before its handler runs; every request
schema is a strict object with no open `unknown` outside a reviewed list; nothing listens on an event
channel; the production CSP keeps scripts to the app and checked plugin modules; navigation and popups are
denied; webview attach is forced to our hardened guest settings; the media scheme, external opener, deep
links and permission policy refuse what they must. The Electron E2E layer (`security-audit.spec.ts`, the
harness's lane) checks the same boundaries in a real window: the CSP is present as meta and as the header
on the app page, frames without the preload see no bridge, the app window cannot navigate away or open a
window, and (when a packaged build is there) the fuses read from the binary are the hardened set.
The web half drives a real `indus dashboard` through Playwright: Host guard on HTML and API (the HTML
carries the loopback token, so a rebinding page must not get it), every WebSocket path refusing an upgrade
with no credential, with a credential in the URL, and from a foreign Origin; the dashboard not being
frameable; gated mode's cookie flags, CSRF refusals (`Origin` foreign and `null`), and the 401 on every
protected path including unknown plugin routes.

Inputs → outputs: IPC events / HTTP requests / WS upgrades / framing attempts → refusal envelopes, status
codes, close codes, response headers.

Files: `apps/desktop/electron/security/audit.test.ts` (new), `apps/desktop/e2e/security-audit.spec.ts`
(new), `apps/desktop/e2e/vitest.config.ts` (include the spec), `apps/web/e2e/auth-gates.spec.ts` (new),
fix in `src/indus/web/spa.py` with `tests/unit/web/test_spa.py` (regression test).

Edge cases / failure modes:
- a channel added to the table later is covered automatically (the audit iterates the table);
- a request schema loosened to `z.object` / `unknown` fails with the channel and path named;
- sync channels answer through `returnValue`; a refusal must still set it (no hang in the preload);
- WS refusals are accepted then closed with a 44xx code (a browser sees the code);
- the gated stack binds `127.0.0.2` (still this machine), never an external interface.

Our design differences / decisions:
- The reference denies every window-open and delegates exactly one hub origin to its opener; we deny all
  with no delegation (our trusted-click path goes through the guest preload's `sendToHost`), already so.
- "Every channel from a foreign frame" is proven at the registry over the full table (unit) because in a
  real Electron window no foreign frame gets an `ipcRenderer` at all (no preload in subframes or guests'
  pages); the E2E layer proves that last part.

Tests we will write: see Files; one `it` per boundary row, parametrized over channels/paths.

## Decisions for the lead
- Finding SEC2-1 (medium, fixed): the dashboard's HTML (index and bundle files) had no framing policy, so
  any site could frame the loopback dashboard (which holds the session token in the page) and clickjack
  it. Now `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'` and
  `X-Content-Type-Options: nosniff` on every SPA response. No surface frames the dashboard (checked: no
  iframe in apps/web or apps/desktop renderer; the desktop preview is a `<webview>`, which is not a frame
  ancestor).
- The SPA still has no full script CSP (its bootstrap is one inline script); a hash-based `script-src`
  is a follow-up, not done here (it touches the bootstrap and the theme CSS).

Compare checklist (filled after the rewrite; reopened `window-open-policy.ts`, `hardening.ts`):
- [x] window-open always denied, no window created, log carries the origin only (test:
  `audit.test.ts > navigation and popups`)
- [x] a throwing logger cannot change the decision (existing `navigation.test.ts`; audit repeats it)
- [x] sensitive files refused over IPC (existing E2E `security.electron.test.ts` fs case; audit pins
  `sensitiveFiles` list via the fs E2E, not duplicated)
- [x] path syntax refusals (NUL, traversal) on the media scheme (test: `audit.test.ts > media scheme`)
- Deliberate differences: no delegated window-open exception (we have no embedded hub frame).
- Not taken: secret-file mode tightening lives in `secrets.ts` with its own tests (not a P25-03 row).

## Status

Done (2026-10-21). `electron/security/audit.test.ts` 94 passed (root vitest lane); `apps/web/e2e/auth-gates
.spec.ts` 5 passed against a real dashboard (Chromium from /opt/pw-browsers); the regression test
`tests/unit/web/test_spa.py::test_no_bundle_answer_may_be_framed` fails on the old `spa.py` (no
`x-frame-options`) and passes now; the framing spec failed with the fix removed and passes with it.
`e2e/security-audit.spec.ts` is in the Electron lane (`pnpm --filter @indus/desktop e2e`) and skipped on
this host (no Electron binary; downloading one is network the wave rules do not allow) — it type-checks
and lints clean but has not run yet: the lead's Electron lane is its first run.

Observations (no fix needed, recorded for the threat model):
- `IpcRegistry.seal()` answers a sync channel nobody registered with `not-available` without the sender
  check (no data leaves; every registered sync channel is checked).
- Gated mode lets an unsafe request without any `Origin` through the CSRF check (non-browser clients);
  browsers always send `Origin` on such requests and the session cookies are `SameSite=Lax`.
- `cookies.read` also accepts the unprefixed cookie name on https (a sibling-subdomain cookie could plant a
  session of the attacker's own, never read the person's); preferring only the `__Host-`/`__Secure-` name
  when served over https is a small follow-up.
- On a loopback bind a WebSocket from another loopback port's page passes the Origin rule; it still needs
  the session token, which only the dashboard's own page has.

Integration (wave 43, 2026-10-22, integrator): landed on main `bee44d9`. First run of
`e2e/security-audit.spec.ts` in the Electron lane (Electron 44.5.1, zip checked against SHASUMS256
and `checksums.json`, xvfb): see LEAD-NOTES "P25 wave 43 landing notes (integrator)" for the counts;
`auth-gates.spec.ts` ran in the web lane with `CI=true`. The observations above are finding SEC-18
of the threat model (open, small follow-ups); the framing fix is SEC-16 / D-16.906, the suites
D-16.907.

## Wave 45 (S45 — SEC-18, D-16.972 – D-16.975)

Status: done (2026-10-22): the four observations above are fixed. Read: no reference file.

- `IpcRegistry.seal()`: a sync channel left without a handler now runs the sender check before
  answering `not-available` (as invoke channels already did). Test: `audit.test.ts` "channels left
  to seal() refuse every foreign frame too (SEC-18)" (fails on the old registry, 9 cases).
- Gated CSRF without `Origin` (`web.auth.gate.same_site_request`): an unsafe method needs a
  same-origin `Origin`; without one, a same-origin `Referer`; with neither it is refused 403
  `forbidden_origin`. The one non-browser credential gated mode documents and takes is the
  service bearer on its opted-in routes (`INDUS_DASHBOARD_DRAIN_SECRET`, `auth.service_bearer`),
  decided by `BearerSeam` before this check, so it is unaffected; the session token is refused in
  gated mode anyway. The dashboard test client now sends the page's `Origin` like a browser
  (`build(browser=False)` sends none).
- Cookies (`web.auth.cookies.read`): over https only `__Host-` (first) and `__Secure-` (proxy
  prefix) names are read, never the plain name; over http every variant as before.
- SPA script CSP (`web.spa.index_csp`): the index answer carries `frame-ancestors 'none';
  script-src 'self' 'sha256-<bootstrap>'; object-src 'none'; base-uri 'self'`, the hash computed
  per answer over the exact bootstrap text (its globals differ by prefix and mode). The bootstrap
  stays inline on purpose: an external script URL holding the loopback session token could be
  pulled in by any page's `<script src>` and read.

Tests: `tests/security/test_trust_boundaries.py::test_web_gated_unsafe_request_needs_its_own_origin_or_referer`,
`::test_web_https_session_cookie_only_under_its_prefixed_name`,
`::test_web_dashboard_scripts_only_from_its_origin_and_the_hashed_bootstrap` (each fails on the old
code), `tests/unit/web/auth/test_cookies.py`, `tests/unit/web/test_spa.py`.
