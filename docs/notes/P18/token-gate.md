# Task: loopback token gate, public allowlist, Host guard, middleware order (P18-02) Phase: 18 Owner: builder W1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/web_server.py` (lines
~200–520), `hermes_cli/dashboard_auth/public_paths.py`, `hermes_cli/dashboard_auth/middleware.py`,
`hermes_cli/web_server_boundary.py` (Host/Origin/peer rules), `hermes_cli/dashboard_auth/body_limit.py`.
Evidence: none.

What it does (own words): decides whether the dashboard runs in loopback mode or gated mode and
enforces it on every request. Gated mode engages when the bind host is outside the loopback set
(`127.0.0.1`, `::1`, `localhost`) or when a declared public URL's host is not loopback; LAN,
CGNAT (100.64/10) and link-local addresses are public. Loopback mode mints a 32-byte URL-safe
session token at start (or takes the desktop's from `INDUS_DASHBOARD_SESSION_TOKEN`) and requires it
on every `/api/*` request outside the public allowlist, in `X-Indus-Session-Token` or
`Authorization: Bearer`, compared in constant time. Gated mode never injects the token and refuses it
when presented. One allowlist constant (health, status, config defaults/schema, model info, theme
list, plugin manifest list, cron fire webhook) is used by both gates. The Host guard defends
against DNS rebinding: the exact bound host (with or without port), loopback aliases when bound to
loopback, exact declared public hostnames, anything when bound to all interfaces; otherwise 400 JSON;
never a wildcard or a suffix match. Order, outermost first: service-bearer seam → login gate
(loopback token or cookie session) → plugin-route gate (only after auth, so a caller without a
credential always gets 401 and never a 404 that reveals plugin names) → Host guard → 64 KiB body limit
on the public auth POSTs. A gated start without any provider: interactive → offer to set a
username/password (written through the config writer), non-interactive → refuse to bind, listing each
provider's reason.

Inputs → outputs: bind host, public URL, env, request headers → `GateMode`; per request: pass,
401 JSON (`{error, detail}`, gated adds `login_url`), 302 to `/login` for HTML in gated mode, 400
JSON for a bad Host, 413 JSON for an oversized auth body.

Files: `src/indus/web/auth/{__init__,gate,token,public_paths,host_guard}.py`,
`tests/unit/web/auth/test_gate.py`, `tests/unit/web/auth/test_host_guard.py`.

Edge cases / failure modes:
- Host header with port, IPv6 brackets, unbracketed IPv6 (ambiguous → refused), userinfo, path,
  quotes, whitespace → refused; empty Host → refused.
- `0.0.0.0` / `::` bind: any well-formed Host accepted (no defence possible at this layer).
- desktop token in env shorter than 32 characters or outside the URL-safe alphabet → refuse to start.
- `/api/status/extra` is not public (exact match); `/assets/x` passes the gated gate only as a
  static prefix; `/api` itself requires the token.
- token compare: different lengths compare in constant time (digest first).
- body limit: by Content-Length up front, and by counting streamed chunks.

Our design differences / decisions:
- Plugin API routes use the prefix `/api/ext/<name>/` (the plugins REST page of P18-14 owns
  `/api/plugins/...`); dashboard plugins are later (L17), so the gate refuses every one (404) once
  authenticated; the seam is a predicate in the state.
- Gated mode answers 401 when a request carries the session-token header at all, or a Bearer equal
  to the session token (rejected even if presented, D-16.25).
- CSRF: in gated mode an unsafe method (POST/PUT/PATCH/DELETE) whose `Origin` is present and not an
  accepted host gets 403; cookies are SameSite=Lax as well.
- Only the files-download route may carry `?token=` (P18-16); the gate keeps that one-path constant.

Tests we will write:
- Table: bind × public URL → gate on/off (192.168.x, 100.64.x, 169.254.x, 0.0.0.0, ::, ::1,
  localhost, a hostname; public URL loopback / LAN / name).
- token accepted on loopback (header and Bearer), wrong/missing refused; rejected when gated.
- the allowlist is the same object in both gates (identity assert).
- unknown plugin path without credential → 401, with credential → 404.
- Host `evil.com` → 400; suffix tricks refused; IPv6 forms.
- body over 64 KiB on `/auth/password-login` → 413, other routes unaffected.
- non-interactive gated start without a provider exits non-zero listing the reasons (CLI).

## Decisions for the lead
- Plugin route prefix `/api/ext/<name>/` (above).
- Unsafe-method Origin check in gated mode (above).
- The desktop-supplied token must be ≥ 32 URL-safe characters, else the server refuses to start.

Compare checklist (filled after the rewrite):
- [x] gate engagement: bind host OR public URL host (test: `test_gate.py::test_gate_table`)
- [x] allowlist single constant, exact match
- [x] middleware order
- [x] Host rules incl. all-interfaces and public hosts
- [x] body limit only on public auth POSTs

Compare (integrator, 2026-10-20; reference reopened read-only, then closed): `hermes_cli/web_server.py` (~200–520: token checks, middleware order, query-token path), `hermes_cli/dashboard_auth/public_paths.py`, `middleware.py`, `body_limit.py`, `hermes_cli/web_server_boundary.py`.
- [x] public list: the same eight exact paths
- [x] gated bypass prefixes: ours lack `/auth/native/*` (no native broker, D-16.589), `/dashboard-plugins/`, `/ds-assets/`, `/fonts-terminal/` (no such mounts yet) and `/api/mcp/oauth/callback/` (P18-14 follow-up)
- [x] Host rules identical (exact bound host, loopback aliases, public hosts, any on all interfaces, authority syntax fail-closed)
- [x] gate engagement: bind outside loopback or a non-loopback public host; the reference's desktop loopback exemption is not built (D-16.589, open for P20-10)
- [x] body limit: 64 KiB on public `/auth/` POSTs, by header and while streaming
- [x] query token only on `/api/files/download` (same as the reference)

## Status

Done (2026-10-20), builder W1. Compare: done by the P18 integrator with the reference reopened (see the integrator Compare above).
