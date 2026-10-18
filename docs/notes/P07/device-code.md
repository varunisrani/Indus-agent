# Task: Device-code flow, RFC 8628 (P07-09) Phase: 07 Owner: P07 slice B builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/auth_device_flow.py`
(polling loop, instruction block, TLS choice), `hermes_cli/auth_oauth_grants.py` (refresh skews,
dead-grant errors; grep only); research `04-cli-config.md` §3.16.4; trap R04.17. Closed before
writing. Standards checked 2026-10-18: RFC 8628 §3.1–3.5 (fields `device_code`, `user_code`,
`verification_uri`, optional `verification_uri_complete`, `expires_in`, optional `interval`
defaulting to 5; grant type `urn:ietf:params:oauth:grant-type:device_code`; errors
`authorization_pending`, `slow_down` = +5 s for this and later polls, `access_denied`,
`expired_token`; reduce polling on connection timeouts), RFC 6749 §6 (refresh grant) and §5.2
(error JSON).
Evidence: none.

What it does (own words): a generic poller any provider profile can use when it publishes a device
endpoint. It asks for a device code, shows the verification URL and user code (opening a browser
only when one is likely, P07-10), then polls the token endpoint until approval, denial, expiry or
the deadline. Rate-limit and outage answers that are not OAuth JSON (408, 429, 5xx, an edge-block
403) back off without abandoning a login the person may still be approving. When the deadline
passes while the endpoint was failing, the message says the service was down, not that the person
did not approve (R04.17). Tokens are stored with `obtained_at`/`expires_at`; they are refreshed
120 s before expiry; a dead grant asks the person to sign in again.

Inputs → outputs:
- `DeviceFlowSpec(provider, device_authorization_url, token_url, client_id, scope?,
  slow_down_step=5, doc_url)`.
- `request_device_authorization(client, spec)` → `DeviceAuthorization` (missing required field →
  `DeviceFlowError`).
- `poll_for_token(client, spec, grant, clock=)` → `TokenSet`; errors `AccessDenied`,
  `DeviceCodeExpired`, `SignInServiceDown`, `DeviceFlowError`.
- `refresh_tokens(client, spec, tokens, clock=)` → `TokenSet`; dead grant → `ReloginRequired`.
- `needs_refresh(tokens, now)`; `TokenSet.to_secret()`/`from_secret()` (the JSON kept in the
  SecretStore); `tls_verify(ca_bundle, insecure, warn)`.
- `run_device_login(spec, clock=, say=, open_url=, client=)` ties it together for `auth add`.

Files: `src/indus/providers/auth/device_code.py`, `tests/unit/providers/auth/test_device_code.py`,
`tests/fakes/oauth_server.py`.

Edge cases / failure modes:
- `interval` missing → 5 s; `slow_down` repeatedly → +5 s each, capped at 30 s.
- 503 with `Retry-After: 7` → wait 7 s (at least the interval, at most 60 s, never past the
  deadline); without it → doubling from max(5, 2×interval) up to 60 s.
- A transport error (timeout, connection refused) counts as the endpoint failing.
- A JSON error the RFC does not list → `DeviceFlowError` with the code and description.
- 200 without `access_token` → `DeviceFlowError`.
- Refresh: no refresh token, `invalid_grant`, `invalid_token`, or a "reused" refresh token →
  `ReloginRequired`; a new refresh token in the answer replaces the old one, else the old one stays.
- `--insecure` prints a loud warning; `--ca-bundle` that does not exist → error, never a silent
  fallback.

Our design differences / decisions:
- `slow_down` adds 5 s as RFC 8628 requires (the reference adds 1 s); lead decision, a provider
  profile may document another step through `slow_down_step` (none does today).
- No provider-specific interval cap (the reference caps one portal at 1 s; we have no portal).
- An edge-block 403 is recognised by its non-JSON body (an OAuth error is always JSON), not by one
  CDN vendor's header.
- No bundled provider declares a device endpoint today (D-16.42: key providers only); the poller is
  exercised against the fake server and is ready for the first profile that documents one.

Tests we will write:
- pending ×3 then success (poll times on the fake clock); slow_down growth and cap; 503 with
  Retry-After; 503 without (doubling); outage-at-deadline wording vs plain expiry; access_denied,
  expired_token; refresh skew; dead grant; refresh keeps the old refresh token.

## Decisions for the lead
- none beyond the slow_down step already ruled.

Compare checklist (filled after the rewrite; reopened `hermes_cli/auth_device_flow.py`):
- [x] pending/slow_down handling (tests: `test_device_code.py`)
- [x] edge back-off and outage-at-deadline (tests: `test_device_code.py`)
- [x] transport failures count as an outage (test: `test_device_code.py::test_transport_errors_count_as_an_outage`)
- [x] dead grant / reused refresh token (test: `test_device_code.py::test_dead_grant_asks_to_sign_in_again`)
- Deliberate differences: see above.
- Not taken: the portal-specific timeout copy, reuse-existing-credentials prompt.

## Status

Done (2026-10-18). No bundled provider declares a device endpoint, so `indus auth add --type oauth` reaches this flow only through a future profile entry in `providers/auth/flows.py`; the PKCE fixed-port fallback also uses it. Compare: the reference sleeps on the wall clock and caps one portal's interval at 1 s; we wait on the injected clock and keep RFC pacing.

## Integration (lead)

- The RFC 8628 `slow_down` step (5 s) is recorded as D-16.94.
