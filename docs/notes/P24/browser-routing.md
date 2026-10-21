# Task: hybrid routing of private URLs (P24-41) Phase: 24 Owner: wave-38 builder W1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/browser_tool_origin.py`
(only a module-lookup helper; nothing about routing), the `_auto_local_for_private_urls`,
`_is_local_backend` and `_allow_private_urls` parts of `tools/browser_tool_cloud.py`; research 02
§3.17 navigation safety, §3.25 hybrid routing. Closed before writing. Evidence: none.

What it does (own words): with a cloud browser provider configured and
`browser.route_private_local: true`, a navigation whose address is private, loopback or link-local
(decided by the P05 URL policy after DNS resolution) is opened in a local Chromium "sidecar"
session of the same task instead of the cloud browser, so the provider never sees the address. The
SSRF policy is relaxed only for that sidecar: its request guard allows private addresses, but the
cloud-metadata floor (169.254.0.0/16 and the named metadata hosts) stays refused. A public URL
that redirects to a private address is stopped on the cloud route by the guard (or the landing
check) and, only when routing is on, retried on the sidecar. Later page tools of the task follow
the route the last navigation took. Each navigate result names the engine that served it
(`cloud:<provider>`, `local-sidecar`, …). With routing off, a private URL is refused exactly as
before.

Inputs → outputs: `browser.route_private_local` (default false), `browser.provider` → navigate
results with `engine`; `SessionManager` keeps the task's current route.

Files: `src/indus/tools/builtin/browser/routing.py`, test `test_routing.py`; shared:
`browser/session.py` (route per task, sidecar sessions, relaxed gate), `browser/tools.py`
(navigate consults the router), schema row `browser.route_private_local`.

Edge cases / failure modes:
- metadata addresses never go anywhere (both routes refuse them);
- no cloud provider → no routing (local sessions keep the normal policy);
- a public page whose sub-resource is private is not rerouted (the page loaded; only that request
  was refused);
- the sidecar closes with its task; a provider fallback to local leaves the task on local Chromium
  under the normal policy (routing only applies while the cloud serves the task).

Our design differences / decisions:
- Opt-in (default false; the reference defaults on): routing relaxes the private-address rule for
  the sidecar, so a cloud user would otherwise get a looser policy than a local user.

Tests we will write:
- `http://192.168.1.10` goes local with routing on; blocked with routing off;
- public URL redirecting to `10.0.0.1`: stopped on the cloud route, retried locally only when on;
- metadata refused even with routing on; later snapshot follows the local route; engine label.

## Decisions for the lead
- Default `browser.route_private_local: false` (see above); flip to true for reference parity.

Compare checklist (filled after the rewrite; reopened the files above):
- [x] private URL routed local only with a cloud provider and the switch (tests: `test_routing.py::test_private_address_goes_local_when_routing_is_on`, `::test_routing_off_blocks_private_address`)
- [x] redirect re-checked after the cloud navigation (test: `::test_public_url_redirecting_private_is_retried_locally`)
- [x] metadata floor holds on the local route (test: `::test_metadata_is_refused_even_with_routing`)
- Deliberate differences: opt-in default; per-task route memory instead of a last-session key.
- Not taken: routing for `browser_exec` (its CLI runs locally already).

## Status

Done (2026-10-21).
