# Task: OIDC and service-bearer providers (P18-04) Phase: 18 Owner: builder W1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/dashboard_auth/native_flow.py`
(PKCE part), `hermes_cli/dashboard_auth/refresh_singleflight.py`, `hermes_cli/dashboard_auth/native_http.py`
(bearer route opt-in), `website/docs/user-guide/features/web-dashboard.md` (Authentication section).
Integrator Compare also read: `hermes_cli/dashboard_auth/token_auth.py`.
Evidence: none.

What it does (own words): a self-hosted OpenID Connect provider for gated mode. Config: issuer and a
public client id (no client secret), scopes (default `openid profile email`), ID-token leeway. The
provider fetches `<issuer>/.well-known/openid-configuration`; the issuer and every endpoint must be
HTTPS unless they are loopback; the discovered issuer must equal the configured one (a trailing slash
is tolerated). Login: authorization code + PKCE S256 (+ a nonce); the callback swaps the code at the
token endpoint and verifies the ID token (RS256 or ES256 only) against the keys at `jwks_uri`, with
`iss` and `aud` pinned and a 60 s leeway on the time claims (0 = strict; an invalid value fails closed
to 0). Claims: `sub` → user id, `email`, display name from name → preferred_username → nickname →
email, org from `org_id`/`organization` else the joined groups. A session that expired is renewed with
the refresh-token grant, one grant at a time per refresh token (concurrent requests share the
result). Logout revokes the refresh token when the IdP advertises an RFC 7009 revocation endpoint.
Service bearer: a provider may opt specific routes into `Authorization: Bearer` (exact paths); the
bundled drain route stays disabled unless its secret has at least 256 bits. No vendor portal
provider (D-16.12, L14).

Inputs → outputs: config `dashboard.oidc.{issuer,client_id,scopes,id_token_leeway}` (env
`INDUS_DASHBOARD_OIDC_ISSUER`, `INDUS_DASHBOARD_OIDC_CLIENT_ID` win when non-empty) → provider or a
reason; `/auth/login?provider=oidc` → 302 to the IdP with the PKCE cookie; `/auth/callback` → session
cookies; `INDUS_DASHBOARD_DRAIN_SECRET` → bearer provider on the drain route or a disabled route.

Files: `src/indus/web/auth/{oidc,service_bearer}.py`, `tests/unit/web/auth/{test_oidc,test_service_bearer}.py`,
fixture `tests/fakes/idp/fake_oidc.py` (in-process ASGI IdP with its own JWKS, discovery, token and
revocation endpoints; keys generated at runtime, nothing committed).

Edge cases / failure modes:
- discovery unreachable / non-JSON / missing fields → provider unavailable (503), never a logout.
- issuer mismatch, `http://` issuer that is not loopback, an `http://` endpoint → refused at load.
- ID token: `alg` none/HS256 refused; unknown `kid` → one JWKS refetch at most every 5 minutes;
  expired inside the leeway accepted, outside refused; wrong `aud`; wrong `iss`; nonce mismatch.
- refresh: refused by the IdP → session expired (cookies cleared); IdP down → 503, cookies kept.
- service bearer: missing/short/wrong token → 401 on that route only; other routes never accept it.

Our design differences / decisions:
- Our session cookie carries our own signed token (shared signer), not the IdP's access token; the
  IdP's refresh token is the only provider token stored (HttpOnly cookie).
- JWKS fetched with our httpx client and turned into keys with PyJWT (`PyJWK`); PyJWT does the
  signature maths (no hand-written crypto).
- Single-flight keyed by the SHA-256 of the refresh token, results kept 30 s (failures 5 s).
- Secret strength: bits = length × log2(alphabet), the alphabet being the smallest of hex (16),
  base64url (64) or printable ASCII (95) that holds every character.

Tests we will write:
- fake IdP: issuer mismatch refused; http issuer refused unless loopback; expired token inside leeway
  accepted, outside refused; wrong `aud` refused; claims mapping; full login round trip through the
  app; two concurrent refreshes → one grant call; logout revokes.
- bearer: valid secret opens the drain route; wrong token 401; the route never accepts a cookie
  alone; a 255-bit-class secret disables it (404).

## Decisions for the lead
- Env names `INDUS_DASHBOARD_OIDC_ISSUER`, `INDUS_DASHBOARD_OIDC_CLIENT_ID`,
  `INDUS_DASHBOARD_DRAIN_SECRET`; config under `dashboard.oidc`.
- Drain route path `/api/daemon/drain` (P18-15 mounts the handler; the seam owns the auth).
- A nonce is added to the authorization request and checked in the ID token.

Compare checklist (filled after the rewrite):
- [x] PKCE S256 pair and state/verifier in the PKCE cookie
- [x] refresh single-flight semantics (success and failure caching)
- [x] bearer route opt-in exact paths, fail closed
- [x] docs rules: https, issuer equality, leeway, claims mapping, revocation

Compare (integrator, 2026-10-20; reference reopened read-only, then closed): `hermes_cli/dashboard_auth/native_flow.py`, `refresh_singleflight.py`, `native_http.py`, `token_auth.py`, `website/docs/user-guide/features/web-dashboard.md` (Authentication, public URL).
- [x] S256 transform and PKCE pair; success 30 s / failure 5 s single-flight windows, keyed by token digest
- [x] bearer seam: exact opted-in paths, outermost, 401 / 503, fail closed
- [x] gap fixed at integration: `INDUS_DASHBOARD_OIDC_SCOPES` (the docs' env for scopes) now wins over config when set (D-16.588, `test_env_values_win_over_config_when_set`)
- [x] not built (D-16.589): the RFC 8252 native broker (`/auth/native/*`), the native header grant, a provider access token on gated WS upgrades

## Status

Done (2026-10-20), builder W1. Compare: done by the P18 integrator with the reference reopened (see the integrator Compare above).
