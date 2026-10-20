# Task: public URL, trusted proxies and path prefix (P18-05) Phase: 18 Owner: builder W1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/dashboard_auth/prefix.py`,
`hermes_cli/dashboard_auth/request_utils.py`.
Evidence: none.

What it does (own words): makes the dashboard work behind a reverse proxy. A declared public URL
(`INDUS_DASHBOARD_PUBLIC_URL`, else `dashboard.public_url`) fixes the OAuth callback to
`<public_url>/auth/callback` verbatim — the prefix header is ignored there — and adds its host to the
hosts the Host and Origin checks accept. A malformed value (no http/https scheme, no host, quotes,
angle brackets, whitespace, control characters) is ignored with a warning (once per value).
`X-Forwarded-Proto` and `X-Forwarded-For` are believed only from a loopback peer or a listed proxy
(exact IPs or bounded CIDRs); `*`, `0.0.0.0/0` and `::/0` are refused when the setting is loaded, so
the dashboard does not start with them. `X-Forwarded-Prefix` (also only from trusted peers) rewrites
the absolute `/assets/`, `/fonts/` and favicon URLs in `index.html` and `url(/…)` inside served CSS at
request time, and the base-path global tells the SPA to prefix every REST/WS URL and its router.

Inputs → outputs: public URL string, `dashboard.trusted_proxies` list, request headers → the
callback URL, the accepted host set, the effective client IP and scheme (`scope["client"]`,
`scope["scheme"]` rewritten for trusted peers), the prefix used for rewriting, cookie Path and
redirects.

Files: `src/indus/web/auth/proxy.py`, `src/indus/web/prefix.py`, `tests/unit/web/test_prefix.py`,
`tests/unit/web/auth/test_proxy.py`.

Edge cases / failure modes:
- prefix: missing leading slash added; trailing slash dropped; `..`, `//`, quotes, `<>`, whitespace,
  control characters, over 256 characters → ignored (no prefix) with a one-time warning.
- forwarded-for chain: walk from the right, skipping trusted proxies; the first untrusted address is
  the client; garbage entries → keep the peer.
- forwarded-proto other than http/https → ignored.
- CIDR with host bits set, `/0` of either family, a name → refused with the entry named.
- CSS rewriting under a prefix answers `Vary: X-Forwarded-Prefix` so a shared cache cannot serve one
  prefix's CSS to another.

Our design differences / decisions:
- Forwarded headers are applied by our own small ASGI middleware (outermost) instead of uvicorn's
  `proxy_headers`, so the rule is testable and the server always runs with `proxy_headers=False`.
- The prefix header is honoured only from trusted peers, like the other forwarded headers.

Tests we will write:
- the same build served at `/` and under `/indus/`: index asset URLs and CSS `url(...)` rewritten,
  untouched without a prefix; base path global set.
- callback ignores the prefix when a public URL is set, includes it otherwise.
- forwarded headers from a non-listed peer ignored, from a listed peer applied.
- `0.0.0.0/0`, `::/0`, `*` rejected at load; a bounded CIDR accepted.

## Decisions for the lead
- Prefix header trusted only from loopback/listed peers (the reference reads it from anyone).

Compare checklist (filled after the rewrite):
- [x] prefix normalisation rules and the length cap
- [x] public URL validation and precedence (env, then config; blank env = unset)
- [x] client IP never parsed from untrusted headers

Compare (integrator, 2026-10-20; reference reopened read-only, then closed): `hermes_cli/dashboard_auth/prefix.py`, `request_utils.py`.
- [x] prefix: leading slash, no trailing slash, `..`/`//`/injection characters refused, 256-character cap; ours trusts the header only from trusted peers (D-16.590)
- [x] public URL: env (blank = unset) then config, malformed warns and falls through; callback `<public_url>/auth/callback` verbatim
- [x] client IP from the ASGI peer after trusted forwarding only; safe `next` refuses `//`, `/login`, `/auth/`, `/api`

## Status

Done (2026-10-20), builder W1. Compare: done by the P18 integrator with the reference reopened (see the integrator Compare above).
