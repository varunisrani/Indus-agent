# Task: gated mode — username/password provider, cookies, audit log (P18-03) Phase: 18 Owner: builder W1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/dashboard_auth/base.py`,
`registry.py`, `cookies.py`, `audit.py`, `routes.py` (login/logout/whoami routes), `token_auth.py`.
Evidence: none.

What it does (own words): the login side of gated mode. A registry holds several providers in
order; the login page lists them. The username/password provider protects one credential: an
argon2id hash in `dashboard.basic_auth.password_hash`, or a plaintext `INDUS_DASHBOARD_PASSWORD` in
the environment, which wins over the hash so the password can be rotated without editing config.
A successful login mints a stateless session token signed with HMAC-SHA256: TTL 43,200 s, signing key
from `INDUS_DASHBOARD_SESSION_SECRET` or random per process (then sessions end on restart). Login is
rate-limited to 10 attempts a minute per client IP (429); an unknown user and a wrong password give
the same 401 body and take the same path (a dummy hash is verified for an unknown user). Cookies:
`indus_session_at` (HttpOnly, SameSite=Lax, Secure on HTTPS, Max-Age = TTL), `indus_session_rt` (30
days, only when a provider returned a refresh token), `indus_session_provider`, `indus_session_pkce`
(10 min; SameSite=None+Secure on HTTPS, Lax on loopback HTTP), `indus_sso_attempt` (60 s);
`__Host-` (HTTPS, root path) and `__Secure-` (HTTPS under a prefix) variants written and recognised.
Identity endpoints: who-am-I (`/api/auth/me`), providers list (`/api/auth/providers`), logout (clears
every auth cookie, revokes a refresh token where the provider can, redirects to `/login`). The audit
log appends one JSON line per login start/success/failure, session-verify failure, logout and ticket
event to `<home>/logs/dashboard-auth.log` (0600, rotated at 5 MiB × 3) with tokens, codes,
verifiers, state, passwords and Authorization dropped and any credential shape in other values
masked. Unauthenticated API calls get `{error: unauthenticated | session_expired, detail, login_url}`.

Inputs → outputs: config `dashboard.basic_auth.{username,password_hash}`, env
`INDUS_DASHBOARD_USERNAME`, `INDUS_DASHBOARD_PASSWORD`, `INDUS_DASHBOARD_SESSION_SECRET` → provider;
`POST /auth/password-login {provider?, username, password, next?}` → `{ok, next}` + cookies, or
401/404/429; `POST /auth/logout` → 302 + cleared cookies; audit lines.

Files: `src/indus/web/auth/{providers,basic,cookies,audit}.py`, `src/indus/web/routes/auth.py`,
`tests/unit/web/auth/{test_basic,test_cookies,test_audit}.py`; shared:
`src/indus/foundation/config/schema.py` (`dashboard.*` rows) and its test.

Edge cases / failure modes:
- hash that is not `$argon2id$…` → provider not configured (reason given), never plaintext compare.
- username configured but neither hash nor env password → provider not configured.
- signed token: wrong signature, bad base64, wrong provider, expired (injected clock), username
  changed since → rejected; a token from another process with a random key → rejected.
- rate limiter: per IP sliding window, bounded number of buckets; an empty IP shares one bucket.
- `next` only same-origin paths outside `/login`, `/auth/` and `/api` (`//evil` refused).
- audit write failure is logged and never fails the login.

Our design differences / decisions:
- argon2id through `cryptography`'s `Argon2id` (already locked; no `argon2-cffi`), PHC strings, our
  parameters: 64 MiB, 3 passes, 4 lanes; verification reads the parameters from the string.
- The env plaintext is compared through SHA-256 digests with `hmac.compare_digest` (no length leak).
- No native-app (desktop OAuth) flow: the desktop uses `indus serve` with its own token (D-16.89).
- The session token is our own signed blob for every provider (OIDC included): the provider's own
  tokens never reach a cookie except the refresh token.

Tests we will write:
- injected clock: valid at 43,199 s, expired at 43,200 s; 11th login in a minute → 429; wrong user and
  wrong password identical bodies; env plaintext beats config hash; audit lines carry no secret (regex
  scan for the password, the token, the hash); cookie attributes per scheme and prefix; logout clears
  every cookie variant.

## Decisions for the lead
- argon2id from `cryptography` instead of `argon2-cffi` (no new package; D-16.44 still holds).
- Env names: `INDUS_DASHBOARD_USERNAME`, `INDUS_DASHBOARD_PASSWORD`, `INDUS_DASHBOARD_SESSION_SECRET`
  (the secret must be ≥ 32 bytes once UTF-8 encoded, else a random key is used and a warning logged).
- `/login` here is a plain built-in page until P18-28 replaces the renderer.

Compare checklist (filled after the rewrite):
- [x] provider protocol and registry ordering
- [x] cookie names, attributes, prefix variants, clear-all on logout
- [x] audit redaction and rotation
- [x] password login: rate limit, generic 401, next validation

Compare (integrator, 2026-10-20; reference reopened read-only, then closed): `hermes_cli/dashboard_auth/cookies.py`, `audit.py`, `routes.py` (login/logout/me/ws-ticket, the password rate limiter), `token_auth.py`, `registry.py`.
- [x] cookie names/attributes/variants and clear-every-variant match; no legacy flat PKCE forms (nothing old to read)
- [x] audit: same file, rotation (5 MiB, 3 backups), field cap; ours also masks credential shapes and registered values; the internal WS credential is now registered too (D-16.580)
- [x] rate limiter: 10 per 60 s per IP, bounded buckets, empty IP shares one bucket
- [x] gap fixed at integration: logout revocation is best effort — a provider raising no longer stops the cookies being cleared (D-16.588, `test_logout_clears_the_cookies_even_when_revoking_breaks`)
- [x] `/api/auth/me` in loopback mode answers the local identity behind the token gate (the reference re-checks the token in the handler; ours relies on the gate)

## Status

Done (2026-10-20), builder W1. Compare: done by the P18 integrator with the reference reopened (see the integrator Compare above).
