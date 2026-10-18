# Task: Loopback PKCE + browser/remote heuristics (P07-10) Phase: 07 Owner: P07 slice B builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/auth_openrouter.py`
(whole), `hermes_cli/auth_device_flow.py` (loopback server, browser heuristics, SSH hint); research
`04-cli-config.md` §3.16.5–3.16.6; trap R04.18. Closed before writing. Standards and vendor docs
checked 2026-10-18: RFC 7636 §4.1–4.2 (verifier 43–128 unreserved characters, `S256` = BASE64URL
of SHA-256 without padding), RFC 8252 §7.3 (loopback redirect on an OS-assigned port), RFC 6749
§4.1 (`state`, authorization code grant); OpenRouter "OAuth PKCE"
(https://openrouter.ai/docs/guides/overview/auth/oauth): `GET https://openrouter.ai/auth` with
`callback_url`, `code_challenge`, `code_challenge_method=S256`, optional `state` returned unchanged
and `key_label`; localhost callbacks on any port; `POST https://openrouter.ai/api/v1/auth/keys`
with JSON `{code, code_verifier, code_challenge_method}` → `{"key": …}`; codes single-use, 10
minutes; 403 = invalid/expired code.
Evidence: none.

What it does (own words): signs in through the person's browser with an authorization code
protected by PKCE. The callback server binds first (port 0, or a provider's fixed port), so the
redirect URI names a port this process already owns; the authorize URL carries an S256 challenge
and a random `state`; the server answers until a code or an error arrives with the right `state`,
or the deadline (at least 5 s) passes, and it is always shut down. A callback with a wrong or
missing `state` is refused and never exchanged. On a remote session (SSH or a browser IDE) it
prints the `ssh -L <port>:127.0.0.1:<port> <host>` line to forward the callback. A browser is only
opened when a graphical one is likely (R04.18). If a provider's fixed port is busy and it also
offers a device flow, that flow is used instead. OpenRouter's documented PKCE key mint is the
first consumer: the result is an ordinary OpenRouter API key, stored like any other key.

Inputs → outputs:
- `make_verifier()`, `challenge_for(verifier)`.
- `PkceFlowSpec(provider, authorize_url, exchange_url, exchange: key_mint|token, client_id?,
  scope?, fixed_port?, extra_params, fallback_device?, doc_url)`.
- `LoopbackServer.bind(port)` → `.redirect_uri`, `.wait(deadline, clock)` → `CallbackResult`,
  `.close()`; busy port → `CallbackPortBusy`.
- `run_pkce_login(spec, clock=, say=, open_url=, env=, client=, timeout=)` → `PkceOutcome`
  (`api_key` or `tokens`); errors `PkceDenied`, `CallbackTimeout`, `PkceExchangeFailed`.
- `browser.is_remote_session(env)`, `browser.graphical_browser_likely(env, platform)`,
  `browser.should_open(env, no_browser)`, `browser.tunnel_hint(port, env, host)`.

Files: `src/indus/providers/auth/pkce.py`, `src/indus/providers/auth/browser.py`,
`tests/unit/providers/auth/test_pkce.py`.

Edge cases / failure modes:
- `$BROWSER=lynx` (or a path ending in a console browser, with arguments) → never auto-open.
- Linux without `DISPLAY`/`WAYLAND_DISPLAY` and no `$BROWSER` → print the URL only; macOS and
  Windows assume a browser.
- A request to any other path → 404, result untouched.
- The deadline passes after a wrong-state callback → the timeout message says one was refused.
- The exchange answer is never logged or echoed.

Our design differences / decisions:
- OpenRouter documents `state`, so we send and check it (the reference put a nonce in the path
  instead); the callback path is fixed `/callback`.
- The headless "paste the code" variant is left out for now (needs P07-01's masked prompt); remote
  sessions get the SSH hint.
- No impersonating client ids or redirect URIs: the only registered PKCE flow is OpenRouter's,
  which needs no client registration (R01.12).

Tests we will write:
- Loopback round trip with a fake authorize step (real HTTP to 127.0.0.1) and a mocked exchange;
  wrong state rejected and never exchanged; busy fixed port → device fallback and → error without
  one; server closed on timeout (port free again); heuristics table over env combinations; tunnel
  hint text; S256 against the RFC 7636 appendix B vector.

## Decisions for the lead
- none.

Compare checklist (filled after the rewrite; reopened `hermes_cli/auth_openrouter.py`,
`hermes_cli/auth_device_flow.py`):
- [x] Port bound before the redirect URI (test: `test_pkce.py::test_round_trip_mints_a_key`)
- [x] Remote hint, browser heuristics (test: `test_pkce.py::test_browser_heuristics_table`)
- [x] wrong state refused, never exchanged (test: `test_pkce.py::test_wrong_state_is_refused_and_never_exchanged`)
- [x] busy fixed port → device fallback / error (test: `test_pkce.py::test_busy_fixed_port_falls_back_to_the_device_flow`)
- [x] server closed on timeout, minimum 5 s (tests: `test_pkce.py::test_server_closed_on_timeout`, `::test_minimum_wait_is_five_seconds`)
- Deliberate differences: see above.
- Not taken: the resolved `webbrowser` controller check (we decide from the environment only).

## Status

Done (2026-10-18). OpenRouter's key mint is registered in `providers/auth/flows.py` (new file of this slice) and offered with `indus auth add openrouter --type oauth`.

## Integration (lead)

- The OpenRouter key mint is also offered by `indus model` / `indus setup` ("Sign in" next to
  "Paste an API key"): `model_picker.sign_in_for` wraps every key-mint row of `flows.FLOWS`.
