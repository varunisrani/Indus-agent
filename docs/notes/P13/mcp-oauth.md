# Task: MCP OAuth (P13-09) Phase: 13 Owner: wave-16 builder N2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/mcp_oauth.py` (flow wiring,
storage, callback, fence), `tools/mcp_oauth_manager.py` (per-server provider, reload, 401 handling),
`tests/gateway/test_gateway_mcp_oauth_unattended.py` (edge list only). Research `07` §3.16, R07.24.
Ours: `mcp/config.py` (`OAuthSettings`), `mcp/connect.py` (`ConnectOptions.auth`, `Connection`),
`foundation/secrets.py` (`SecretStore`, P4 decision: keychain, else 0600 file),
`providers/auth/browser.py` (remote session, tunnel hint, open_url), `providers/auth/pkce.py`
(`LoopbackServer`), `providers/auth/device_code.py` (RFC 8628 poller). SDK: `mcp` 2.3.0
`mcp.client.auth.OAuthClientProvider` (httpx2 auth: discovery of protected-resource and
authorization-server metadata, CIMD or DCR, PKCE S256, token exchange, refresh, 403
`insufficient_scope` step-up), `mcp.client.auth.utils` (discovery URL builders), `mcp.shared.auth`
models. Specs checked 2026-10-19: MCP authorization (2025-11-25), OAuth 2.1 draft, RFC 7636, 7591,
8414, 8628, 8707, 9207, 9728, Client ID Metadata Documents draft.

What it does (own words): a server with `auth: oauth` gets an httpx2 auth handler built on the SDK
provider. Our part is around it:
- **Storage** (`OAuthStore`, the SDK's `TokenStorage`): per profile in the `SecretStore` under
  names derived from the server name (`MCP_OAUTH_<SAFE>_<sha8>_*`; secret names allow only
  `[A-Za-z0-9_]`). Access and refresh tokens and the client secret are separate entries (so exact-
  value redaction knows each one); the token record (type, scope, absolute expiry, client id it was
  minted for), the client registration (minus secret) and the authorization-server metadata are
  JSON entries. Nothing goes to config or logs.
- **Expiry across restarts**: the stored expiry is absolute; a load rebases it, and the provider
  seeds the SDK's expiry so a restarted process refreshes instead of sending a dead token. Stored
  metadata is restored so a refresh goes to the real token endpoint.
- **Refresh fence**: one in-process fence per (home, server). A request that would refresh takes
  it, re-reads the store (another session may have refreshed already) and then lets the SDK
  refresh; it holds the fence until the refreshed request goes out. Within 60 s of the last
  refresh no new refresh is sent (the token is used as it is; a 401 then goes through the normal
  path). Two concurrent refreshes → one request.
- **Background never opens a browser** (R07.24): handlers built for sessions are `background`; when
  the server answers 401/403 and the SDK would start discovery/registration/authorization, the flow
  stops with `McpAuthRequired` ("run `indus mcp login <name>`") before any DCR or browser. It is a
  permanent connect error (subclass of `PermissionError`), so lifecycle parks the server (P13-07's
  probe then retries on its timer only). The SDK's own refresh still runs in the background.
- **Interactive login** (`login()` used by `indus mcp login`): binds the loopback callback first
  (port: `oauth.redirect_port` → the port of a cached registration → ephemeral), builds client
  metadata with that redirect URI (`redirect_host` changes only the host name), prints the
  authorization URL, opens a browser only when `browser.should_open` says so, prints the
  `ssh -L` line on remote sessions, waits `oauth.timeout` (300 s) for the callback; the connect is
  bounded by max(timeout + 15 s, connect_timeout). The callback keeps `iss` (RFC 9207).
- **Client identification**: pre-registered (`oauth.client_id`, secret via
  `oauth.client_secret_ref` from the secret store) is used as is and never re-registered; tokens
  minted for another client id are dropped. Otherwise CIMD when `oauth.client_metadata_url` is set
  and the server advertises support (the SDK decides), else DCR.
- **Device code** (`device_login()`, `indus mcp login --device`): discovers metadata, needs a
  `device_authorization_endpoint`, registers (DCR with the device grant) unless pre-registered,
  then the P07 poller (interval, `slow_down` +5 s); tokens, client and metadata land in the store
  in the SDK's shapes so later refreshes go through the SDK.
- `logout()` removes everything stored for a server (`indus mcp remove`).

Inputs → outputs: (name, `McpServer`, scope) → `IndusOAuthProvider | None` (httpx2 auth) for
`ConnectOptions.auth`; `login(...) -> LoginOutcome`; `device_login(...)`.

Files: `src/indus/mcp/oauth.py`, `tests/unit/mcp/test_oauth.py`. Shared (minimal):
`src/indus/providers/auth/pkce.py` (`CallbackResult.iss`).

Edge cases: server never registered (no tokens) in background → `McpAuthRequired` before network
side effects beyond the first 401; refresh rejected → SDK clears tokens → background: auth required;
pre-registered client id changed → old tokens dropped; a token stored by another session is picked
up at the next fence entry; store failures surface as `McpOAuthFailed`; callback with wrong state
refused by the loopback server; login timeout → clear error naming `oauth.timeout`.

Our design differences / decisions:
- Tokens in the profile `SecretStore` (keychain or 0600 file), not token files under the home.
- Only an explicit `indus mcp login` (or `add --auth oauth`) is interactive; chat/gateway/cron
  connects never prompt, even on a terminal.
- No paste-the-redirect fallback, no dashboard/desktop flow (P18/P20), no provider-specific
  workarounds, no published CIMD document of our own (`client_metadata_url` only when configured).
- Fence is in-process (one process owns a profile's live sessions); cross-process refresh races
  are reduced by re-reading the store under the fence.

Tests planned: fake authorization server on httpx2 MockTransport: PKCE pair valid (challenge =
S256(verifier)); DCR then token exchange; tokens in the fake store, not config; refresh fenced
(two concurrent → one request; second within 60 s not sent); background 401 → `McpAuthRequired`,
no register request, no browser call; expiry survives a reload; pre-registered client used without
DCR; device-code flow polls with the interval (fake clock) and stores tokens; remote session prints
the `ssh -L` hint; end-to-end login against the in-process HTTP MCP fake behind a bearer check.

## Decisions for the lead
- `McpAuthRequired` subclasses `PermissionError` so `connect.is_permanent` parks the server without
  a change to M1's code. P13-07 (N1) may also key its "background 401 → parked" warning on it.
- `oauth.client_metadata_url`, `oauth.scope` are read from the server's `oauth` block (extra keys
  the P13-01 model already keeps); no schema change.
- Secret names `MCP_OAUTH_<SAFE>_<sha8>_{ACCESS,REFRESH,TOKEN,CLIENT,CLIENT_SECRET,META}`.
- Shared (minimal) changes: `providers/auth/pkce.py` keeps `iss` from the callback;
  `foundation/errors.py` codes `mcp.auth_required`, `mcp.oauth_failed`.
- Seam for P13-05/07/08 (N1): `indus.mcp.extras.extras_for(...)` builds `ConnectOptions.auth`
  (with sampling/elicitation kwargs); `/reload-mcp` should rebuild extras so a new handler reads
  the store. P13-07's "background 401 → parked, one warning" can key on `McpAuthRequired`.
- A login replaces the stored tokens before the browser step (a failed login leaves none); the
  reference snapshots and restores. Small follow-up if the owner wants it.

## Compare checklist (after writing)
(reopened `tools/mcp_oauth.py`, `tools/mcp_oauth_manager.py`, the unattended test's edge list)
- [x] SDK provider does discovery, CIMD/DCR, PKCE S256, exchange, refresh, step-up; we only wrap it (`test_login_registers_runs_pkce_and_keeps_tokens_in_the_secret_store`, `test_login_then_background_connect_over_streamable_http`)
- [x] storage per profile, never config/logs; absolute expiry rebased on load (`test_expiry_survives_a_restart`); metadata kept so a cold refresh hits the real token endpoint (same test: the fake 404s the guessed endpoint)
- [x] refresh fenced: concurrent → one request (`test_two_concurrent_refreshes_send_one_request`); one per 60 s (`test_no_second_refresh_within_the_fence_window`); refused refresh → no storm (`test_a_refused_refresh_parks_instead_of_retrying`)
- [x] background never opens a browser or registers (`test_background_without_tokens_requires_login_and_never_registers`); the unattended edge list (gateway discovery never interactive) holds by construction: `auth_for` has no interactive mode
- [x] tokens written by another process picked up when ours are spent (`test_a_sign_in_stored_by_another_process_is_picked_up`; the reference watches the token file's mtime)
- [x] pre-registered client: pinned port, no DCR, secret by reference, tokens of another client dropped (`test_preregistered_client_pins_its_port_and_skips_registration`)
- [x] callback port precedence config → cached registration → ephemeral; `iss` kept (RFC 9207); login wait max(timeout + 15, connect_timeout) (`test_login_timeout_covers_the_callback_wait`)
- [x] remote session hint `ssh -L` (`test_remote_session_prints_the_port_forward_hint`); browser opened only when one is likely
- [x] device code: interval, `slow_down` +5 s, tokens/client/metadata stored (`test_device_login_polls_at_the_interval_and_stores_tokens`)
- [x] provider lock released while the MCP request is in flight (as the reference found necessary)
- Not taken (deliberate): published CIMD document and its pinned port range; paste-the-redirect fallback; dashboard/desktop flows (P18/P20); provider-specific workarounds; dead-registration (`invalid_client`) auto-poisoning and the guard against the SDK's guessed `/register` (follow-ups); cross-process file fence (ours is in-process; the store is re-read under it).

## Status
Done (2026-10-19). `src/indus/mcp/oauth.py`, `tests/unit/mcp/test_oauth.py`,
`tests/fakes/mcp/oauth_server.py`; wiring in `src/indus/mcp/extras.py`.
