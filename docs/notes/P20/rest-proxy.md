# Task: REST proxy in main (P20-14) Phase: 20 Owner: wave-34 builder M

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/main.ts`
(REST helpers 5,353–5,600; API proxy dispatch 15,977–16,580 skimmed), `apps/desktop/electron/api-expected-404.ts`
(read in P20-05). Research 08 §3.4, §3.10, trap R08.4; D-16.89. Our `docs/notes/P18/token-gate.md`,
`profile-scope.md`, `web/routes/files.py` (download tickets). Closed before writing.
Evidence: none.

What it does (own words): the renderer has no credential and no network reach of its own to the
backend's REST API; it asks main with `api.request({method, path, body?, profile?, timeoutMs?})`.
Main:
- accepts only a path under `/api/` on the current connection's origin: no scheme, no `//`, no
  backslash, no `..` segment, no fragment; the URL main builds must keep that origin;
- adds the credential for the connection — the launch's `indus serve` session token locally, the
  saved token for a remote — as `X-Indus-Session-Token` on every attempt; the renderer never sees
  it and no response header is passed back;
- adds `?profile=<name>` (the request's profile, else the connection's) unless the path already
  names one, so every lifecycle/status/settings call is scoped (P18-10);
- times out at 30,000 ms (60,000 ms for the startup calls: status, health, config, config schema,
  profiles) unless the request gives its own (≤ 600,000);
- caps a request body at 256 MiB (attachments); refuses file routes that would read a `.env`-like
  or key-store file into the renderer (the guard P20-29 shares); refuses the download-ticket route
  (downloads go through main, never through a ticket URL in the renderer);
- answers `{kind: "response", status, body}`, or `{kind: "not-found", status: 404, body}` for a
  404 (a value, never an exception); a timeout is `timeout`, a refused connection `unreachable`.

Inputs → outputs: `ApiProxy.request(payload)` → `ApiResult`; errors as `IpcError` codes
(`bad-request`, `forbidden`, `too-large`, `timeout`, `unreachable`, `not-connected`).

Files: `apps/desktop/electron/api/proxy.ts`, `apps/desktop/electron/api/proxy.test.ts`,
`apps/desktop/electron/security/sensitiveFiles.ts` (shared guard; new).

Edge cases / failure modes:
- `http://other/` and `//other/api/x` → refused before any request; `/api/../x` refused.
- no connection yet → `not-connected`.
- non-JSON response body → body as text.
- 401 from the local serve → passed back as a response (the orchestrator decides about re-ensure).
- `/api/files/read?path=~/.env` and `.../id_ed25519` → refused.

Our design differences / decisions:
- One header (`X-Indus-Session-Token`) for both modes: P18 accepts it in loopback mode.
- Download tickets never reach the renderer.

Tests we will write (`proxy.test.ts`, a local HTTP server):
- path `http://other/` refused; every attempt carries the token; the answer holds no header and no
  token; 404 → typed not-found; profile appended (and not doubled); timeout fires at 30 s (fake
  timer); startup path 60 s; body cap; sensitive file paths refused.

## Decisions for the lead
- Startup paths: `/api/status`, `/api/health`, `/api/config`, `/api/config/schema`, `/api/profiles`.
- `/api/files/download-ticket` is refused by the proxy (a main-side download is P20-29's).

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] origin confinement and path walking (test: `proxy.test.ts::refuses another origin and paths that walk out of /api/`)
- [x] credential added by main on every attempt, never returned (test: `::every attempt carries the session token; the answer holds no header and no token`)
- [x] expected 404 typed (test: `::404 is a typed not-found, never an exception; a text body stays text`)
- [x] profile selector from the request or the connection, never doubled (test: `::profile appended from the request, else the connection; never doubled`)
- [x] timeouts 30,000 / 60,000 (test: `::timeout fires at 30 s (fake clock); startup calls get 60 s`)
- [x] credential-bearing routes and secret files refused (test: `::no connection yet → not-connected; credential routes and secret files refused`, `::sensitive file names`)
- [x] real `indus serve` through the proxy (test: `realBackend.test.ts`)
- Deliberate differences: one header for both modes; download/WS ticket routes are not proxied to the renderer.
- Not taken: merged session lists across registered remotes and per-profile remote overrides (N17), multipart upload assembly (P20-19/29 build the upload bodies).

## Status

Done (2026-10-21). `electron/security/sensitiveFiles.ts` is shared with P20-29.
