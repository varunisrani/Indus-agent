# Task: ChatGPT sign-in for OpenAI models (wave 61 M1, D-16.1282) Phase: 08 Owner: builder M1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/auth_constants.py` (Codex
constants), `hermes_cli/auth_codex.py` (device-code login, refresh, token store), `hermes_cli/auth_codex_browser.py`
(loopback PKCE), `agent/codex_headers.py` (account headers), `hermes_cli/codex_models.py` and
`agent/model_metadata.py` (catalog URL and `client_version`), `agent/transports/codex.py` (request-shape notes only),
`agent/codex_runtime.py` and `agent/codex_responses_adapter.py` (searched for header names only).
Evidence: none (no live sign-in; protocol facts are cited to OpenAI's open-source Codex CLI in the constants module).

What it does (own words): a person with a ChatGPT subscription signs Indus in to OpenAI through the same public
client OpenAI's own Codex CLI uses (owner decision D-16.1282), and then picks the `openai-codex` provider: model
calls go to the ChatGPT Codex backend in the Responses shape with the signed-in access token and the account header
the backend asks for. Two ways to sign in: a browser on this machine (authorization code + PKCE, loopback callback
on 127.0.0.1:1455, the port the public client is registered with) and a device code for headless machines (a code
shown here, approved on auth.openai.com). Tokens live only in the profile's secret store; `auth.json` keeps metadata.
The access token is renewed before it runs out, under a lock so two processes never spend the same single-use
refresh token; signing out deletes them.

Inputs → outputs:
- `indus auth add openai-codex` (browser; `--device` for a device code; `--no-browser`, `--timeout`) → a pool row
  `auth_type: oauth`, source `pkce` or `device_code`, tokens in the secret store, `providers.openai-codex` state.
- `indus auth status [PROVIDER]` → per-provider summary (no provider: every provider with credentials or a sign-in).
- `indus auth logout --provider openai-codex` (or the positional form) → rows, secrets and state removed.
- model call on route `openai-codex` → `POST https://chatgpt.com/backend-api/codex/responses` (streamed).
- `indus doctor` → one row per stored sign-in (valid for …, renews on next use, or sign in again).
- Dashboard REST (below).

Files: `src/indus/providers/auth/chatgpt_facts.py` (protocol facts with citations), `providers/auth/chatgpt.py`
(device flow), `providers/auth/chatgpt_wire.py` (claims, headers, body hook; stdlib only, imported at start-up), `providers/auth/device_signin.py` (one start/finish/refresh dispatch for both
device-flow kinds), `providers/auth/renew.py` (locked refresh), `providers/auth/flows.py` (row), `providers/auth/pkce.py`
(redirect host/path), `providers/bundled.py` (profile), `providers/profile.py` (`oauth` auth type,
`request_headers` hook), `providers/transports/responses.py` (hook use, stream-only), `providers/streaming.py`
(stream-only transports), `providers/pool.py` (refresh ahead of expiry), `providers/model_list.py` (catalog shape),
`cli/commands/auth.py`, `cli/doctor/checks.py`, `web/routes/models.py` (device dispatch), new
`web/routes/provider_accounts.py`, tests.

Edge cases / failure modes:
- port 1455 busy (a Codex CLI login running) → the browser flow falls back to the device flow, said so on screen.
- callback with a wrong/missing `state` → 400, counted, never exchanged; one use, then closed; times out.
- device flow: 403/404 while the person has not approved yet → keep polling at the server's interval; transport
  blips back off; 15-minute deadline; 429 on the code request → wait (Retry-After) and retry a few times.
- refresh: `invalid_grant` / `refresh_token_reused` / 401 → sign in again (never adopt another program's login);
  429 → quota, not a dead grant; a peer process already rotated the token → adopt the stored pair.
- a token-shaped value never appears in an error, a log line, argv, a URL, config.yaml or a REST answer.

Our design differences / decisions:
- Browser flow is the default for `auth add` (the brief), device with `--device`; the reference defaults to device.
- Our own identity: `User-Agent: indus/<version>` and `originator: indus-agent`; never Codex CLI's strings.
- No reading of `~/.codex/auth.json` or any other program's saved login (R01.12, P2); the reference adopts it.
- No reserved-tool-name aliasing on the wire (renaming tools is out of scope by the owner decision).
- Refresh ahead of expiry happens in the credential pool's call loop (main agent calls); side calls still renew
  on the first 401 through the same locked path.

Tests we will write: facts/claims parsing; device flow against a fake auth server (pending → approve, 429, blips,
deadline, cancel); browser flow (state check, port busy fallback, exchange); locked refresh (peer adoption, dead
grant, quota); transport headers and body against a fake backend; model catalog parse; CLI add/status/logout; REST
contract (start/poll/cancel/status/sign-out, no token in any answer, write scope).

## Contract for UI

All routes are profile-scoped (`?profile=<name>`, family `/api/providers/login` already in
`PROFILE_SCOPED_FAMILIES`), behind the dashboard session; writes (POST/DELETE) use the write scope (400
`profile_required` on a multi-profile host without `?profile=`). No answer ever carries a token, refresh token, id
token or the device code. Errors are `{"detail": {"code": str, "message": str}}`.

1. `GET /api/model/options` (unchanged shape): the `openai-codex` row has `device_login: true`, `sign_in: true`,
   `configured: true` once signed in. Label "OpenAI (ChatGPT sign-in)".
2. `POST /api/providers/login/start` body `{"provider": "openai-codex", "label"?: str}` → 200
   `{"session_id": str, "provider": "openai-codex", "status": "pending", "message": str, "user_code": str,
   "verification_url": "https://auth.openai.com/codex/device", "expires_in": int (s), "interval": int (s)}`.
   Show `user_code` large and a link to `verification_url`; the person types the code there.
   Errors: 400 `no_device_flow` (provider without a device flow), 404 `unknown_provider`, 502
   `sign_in_unavailable` (OpenAI did not answer / refused; message is safe to show).
3. `GET /api/providers/login/{session_id}` → 200 `{"session_id", "provider", "status", "message"}`; `status` is
   `pending` | `approved` | `denied` | `expired` | `failed` | `cancelled`. Poll every `interval` seconds (not
   faster); stop on anything but `pending`. 404 `no_such_session` (unknown, other profile, or 15 min after the end).
4. `DELETE /api/providers/login/{session_id}` → 200 same shape, `status: "cancelled"` (write scope).
5. NEW `GET /api/providers/login/accounts/{provider}` → 200
   `{"provider": "openai-codex", "label": "OpenAI (ChatGPT sign-in)", "signed_in": bool, "signed_in_at": str|null,
   "flows": {"browser": bool, "device": bool}, "accounts": [{"id": str, "label": str, "source": "pkce"|"device_code",
   "status": "ok"|"exhausted", "expires_at": str|null (ISO), "expired": bool, "plan": str|null}]}`.
   `plan` is the ChatGPT plan name from the token's claims (e.g. "plus", "pro"), else null. 404
   `provider_not_found`.
6. NEW `DELETE /api/providers/login/accounts/{provider}` (sign-out, write scope) → 200
   `{"provider": "openai-codex", "removed": int, "signed_in": false}`: every signed-in credential of that provider,
   its stored tokens and the sign-in state are deleted (API keys and env keys stay). 404 `unknown_provider`, 409
   `inherited_pool` (the sign-in belongs to the default profile; sign out there).
7. `POST /api/credentials/pool` with `provider: "openai-codex"` → 400 `sign_in_only` (it takes no API key).

The browser (PKCE) flow is CLI-only: OpenAI only redirects to `http://localhost:1455/auth/callback` on the machine
that runs `indus`, so a dashboard on another machine cannot use it; the dashboard uses the device flow.

## Decisions for the lead
- (W61-M1-1) Provider id `openai-codex` (alias `chatgpt`), display "OpenAI (ChatGPT sign-in)", vendor row bound to
  `chatgpt.com` (no base-URL override: the token only ever goes there).
- (W61-M1-2) Sign-in REST reuses `/api/providers/login/*` (start/poll/cancel) and adds
  `/api/providers/login/accounts/{provider}` (status, sign-out) in the same scoped family, so the web needs no new
  scoped family.

Compare checklist (filled after the rewrite; reopened `hermes_cli/auth_codex.py`, `auth_codex_browser.py`,
`agent/codex_headers.py`, `hermes_cli/codex_models.py`):
- [x] device code: code request retried on 429 (Retry-After, capped), interval never under 3 s, 403/404 = pending,
  six transport/server failures in a row end it, 15-minute deadline, exchange with the device redirect URI
  (tests: `tests/unit/providers/auth/test_chatgpt.py::test_device_sign_in_polls_at_the_server_interval_then_exchanges`,
  `::test_usercode_429_*`, `::test_server_errors_back_off_and_six_in_a_row_end_the_flow`,
  `::test_transport_errors_count_as_failures`, `::test_the_code_expires_after_the_deadline`)
- [x] browser: fixed port 1455, `localhost` redirect string with a 127.0.0.1 listener, scope and
  `id_token_add_organizations`, state checked (now constant-time), one exchange, busy port → device code
  (`::test_browser_sign_in_checks_state_and_exchanges_once`, `::test_browser_flow_falls_back_to_the_device_code_when_the_port_is_busy`,
  `::test_the_listener_binds_loopback_while_the_redirect_says_localhost`)
- [x] refresh: 429 is not a dead grant (`RefreshThrottled`); `invalid_grant` / `refresh_token_reused` (OpenAI's nested
  error shape) / 401 → sign in again; a peer that already rotated the pair is adopted; two sessions at once spend
  the refresh token once (`tests/unit/providers/test_chatgpt_backend.py::test_two_sessions_renewing_at_once_*`,
  `::test_a_dead_grant_*`, `::test_a_throttled_renewal_is_not_a_dead_grant`, `::test_a_pair_renewed_elsewhere_*`)
- [x] refresh ahead of expiry before a request (`::test_a_token_about_to_expire_is_renewed_before_the_request`,
  `::test_the_call_loop_sends_the_renewed_token`)
- [x] account header from the `chatgpt_account_id` claim, residency header, nothing for a non-JWT token, never a
  header with a line break (`test_chatgpt.py::test_account_headers_*`, `::test_a_claim_with_a_line_break_*`)
- [x] request: streamed, `store: false`, no output cap / sampling fields, instructions always present; a
  non-streaming caller still streams (`test_chatgpt_backend.py::test_request_is_streamed_*`,
  `::test_a_non_streaming_call_still_streams_to_the_backend`)
- [x] model catalogue: `client_version`, hidden entries dropped, priority order, account header; default list when it
  cannot be read (`::test_catalogue_shape_*`, `::test_list_models_*`)
- [x] gap found: the generic "looks like a placeholder" key rule (`xxx` anywhere) would drop about one real sign-in
  token in twenty — fixed by not applying it to sign-in tokens (`pool.key_of`), pinned by
  `::test_a_token_that_happens_to_contain_xxx_is_still_used`
- [x] CLI add (browser default, `--device`), `status` without a provider, `logout --provider`, doctor rows
  (`tests/unit/cli/commands/test_auth_chatgpt.py`)
- [x] REST start/poll/cancel/status/sign-out, write scope, session required, no token/device id/verifier in any
  answer (`tests/contract/web/test_provider_accounts_routes.py`)
- Deliberate differences: browser is the default (the reference defaults to the device code); our own
  `originator`/User-Agent; a 403 from the token endpoint is not read as "sign in again" (an edge block looks the
  same); the backend gets a neutral `instructions` line when a side call has no system prompt.
- Not taken: adopting the Codex CLI's saved login (`~/.codex/auth.json`) — forbidden (R01.12, P2); quota probe and
  pool cooldown clearing; the 1 MiB auth-body cap; `-900k` context variants; reserved tool-name aliasing on the
  wire; a dashboard browser (PKCE) flow (the client only redirects to localhost:1455 on the machine running indus).

## Status

Done (2026-10-25), uncommitted in the w61m1 worktree. Shared files outside the slice: `src/indus/web/routes/models.py`
(two calls now go through `device_signin`, docstring), `tests/contract/web/test_profiles_models_routes.py` (route
pin includes the new module), `apps/web/src/test/routes.generated.ts` (regenerated: two new rows),
`docs/internals/traps-ledger.md` (R01.12 test names), `tests/contract/cli/non_tty_table.yaml` (`auth status` → 0),
`docs/user/reference/{cli,providers}.md` (regenerated by `scripts/docs/gen_reference.py`).
The web/desktop UI for the contract above is the UI builder's.
