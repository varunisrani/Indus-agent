# Task: Shared ingress (P24-04) Phase: 24 Owner: wave-37 builder M1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/platforms/shared_ingress.py`
(all). Research 06 §3.25 (shared ingress). Closed before writing.
Evidence: none.

What it does (own words): under multiplexing only the default profile binds HTTP listeners (API
server, webhook receiver, callback adapters). A secondary profile's port-binding apps are mounted
on the default listener under `/p/<profile>/…`: the shared front strips the prefix and hands the
request to that profile's own app, which checks it with that profile's own key or secret, inside
that profile's scope. Un-prefixed paths keep going to the default profile's app untouched. A
prefix naming an unknown profile, the default profile, or a profile with no app for that path gets
404 — never the default profile's handler. API keys and webhook secrets are per profile because
each mounted app is built from its own profile's scope and secret store.

Inputs → outputs:
- `SharedIngress(default_app)`: `mount(profile, name, app, paths=, scope=)`, `unmount(profile,
  name=None)`, `mounted(profile)`, ASGI `__call__`.
- `ApiServer(ctx, front=...)` (api_server.py): the listener serves `front(app)`, so
  `SharedIngress` sits outside the default profile's bearer gate.
- `profile_url(base, profile, path)` for log lines and status.

Files: `src/indus/gateway/multiplex/ingress.py`,
`tests/contract/gateway/multiplex/test_shared_ingress.py`; shared:
`src/indus/gateway/http/api_server.py`.

Edge cases / failure modes:
- `/p`, `/p/`, `/p/<bad id>` (upper case, dots, encoded slash) → 404.
- `/p/default/...` → 404 (the default profile is served un-prefixed).
- profile mounted but no app claims the tail → 404; first app whose paths claim the tail wins.
- WebSocket scopes get the same dispatch (a missing mount closes the socket).
- the forwarded scope keeps the query string; `root_path` gains the prefix; `raw_path` is
  rewritten to match.
- unmount while a request runs: the running request finishes; new ones get 404.

Our design differences / decisions:
- ASGI (starlette) instead of an aiohttp sub-application: our listeners are ASGI already.
- Mounts declare the paths they claim (exact or prefix ending in `/`), because an ASGI app cannot
  be asked whether it routes a path without calling it.
- The request runs under the mounted profile's `Scope` (`use_scope`), the P24-02 rule for
  request handling.

Tests we will write:
- webhook signed with B's secret: accepted at `/p/b/webhooks/x`, 401 at `/webhooks/x`; A's secret
  refused at `/p/b/...`.
- unknown profile → 404; default prefix → 404; B mounted without that path → 404 and the
  default's handler never runs.
- API keys per profile: B's key at `/p/b/v1/models` 200 and 401 un-prefixed; A's key 401 at
  `/p/b/...`; `ApiServer(front=)` serves the front.
- the mounted app runs under B's scope; query strings survive.

## Decisions for the lead
- Wiring owed: the daemon builds one `SharedIngress` per default listener (API server through
  `ApiServer(front=)`, the webhook receiver by `receiver.app = ingress(receiver.app)` before
  `start`), and each served secondary's HTTP parts are built with `serve=False` (callback kit) /
  without binding and mounted with their paths; the kit needs a shared-listener mode flag (P16
  file) for the adapters to skip their own bind.

Compare checklist (filled after the rewrite; reopened `gateway/platforms/shared_ingress.py`):
- [x] default path untouched (test:
  `tests/contract/gateway/multiplex/test_shared_ingress.py::test_b_secret_is_accepted_under_its_prefix_and_refused_unprefixed`)
- [x] missing adapter → 404, never the default (test:
  `test_shared_ingress.py::test_a_profile_without_that_app_gets_404_not_the_default`)
- [x] request runs under the named profile's scope (test:
  `test_shared_ingress.py::test_the_mounted_app_runs_under_its_profile_scope`)
- [x] default/unknown profile yields nothing (test: `test_shared_ingress.py::test_unknown_and_default_prefixes_are_404`)
- Deliberate differences: no frozen sub-app or per-adapter body cap hand-off (each mounted ASGI
  app applies its own length gate); no status-file `ingress_url` write (lead wiring can log
  `profile_url`).
- Not taken: wildcard-host URL helper beyond `profile_url`.

## Wiring (wave 42, builder WA, 2026-10-21)

Read: no reference file (wiring). The host's HTTP part passes `HttpHost.front` (the API server
through `ApiServer(front=)`, the webhook receiver by `receiver.app = front("webhook", app)` before
it starts); a served secondary's HTTP part passes `HttpHost.mount` instead: nothing binds, its API
app (`API_PATHS`: `/health`, `/v1/`, `/api/`) and webhook receiver (`WEBHOOK_PATHS`:
`/webhooks/`) are mounted at `/p/<profile>/` under its scope, status row `{"mounted": "/p/<p>/"}`,
unmounted when it stops. Test:
`tests/integration/gateway/multiplex/test_two_profiles_one_gateway.py::test_a_served_profile_http_is_reached_under_its_prefix_with_its_own_keys`.

Decisions for the lead: a secondary's surface needs the launch profile to enable that listener;
otherwise it is reported as a problem ("no … listener to share") and not served (no listener of
its own under multiplexing). The callback-kit adapters' shared-listener mode is not wired (none of
the multiplex demos needs it; follow-up).

## Status

Done (2026-10-21), wired in wave 42 (API server and webhook receiver).
