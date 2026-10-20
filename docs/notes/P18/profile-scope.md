# Task: Profile scope per request (P18-10) Phase: 18 Owner: wave-29 builder W2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `web/src/lib/api.ts` (header section
through `buildWsUrl`: the management profile, the profile-scoped prefix list),
`hermes_cli/web_routers/_common.py` (profile helpers: the destructive-route rule). Research 09
trap R09.20. Our `foundation/scope.py`, `foundation/profiles.py`, `foundation/bootstrap.py`
(`attach_profile_state`), `foundation/envload.py`. Closed before writing.
Evidence: none.

What it does (own words): one dashboard backend serves every profile under the machine's root.
Each request names its profile with `?profile=<name>`; a FastAPI dependency turns that into a
`Scope` (home, env view, config view, secret store) for the request. A profile that does not
exist (or is deleted, or is not a valid id) answers 404 and creates nothing. Reads without a
profile use the backend's own profile. Writes and destructive routes on a host with more than one
profile refuse a request without an explicit profile (400): an unnamed write would land on
whichever profile the backend was launched with (R09.20). Route modules take their scope only from
these dependencies and never read the home or the process env themselves (architecture rule 4).

Inputs → outputs: `ProfileScopes(base)` installed on the app (`install(app, scopes)`);
dependencies `read_scope` / `write_scope` → `Scope` or `HTTPException(404 | 400)`;
`unscoped_write_routes(app)` → the write routes that do not depend on `write_scope` (used by the
route-introspection test, and by later route tasks).

Files: `src/indus/web/scope.py`, `tests/unit/web/test_scope.py`.

Edge cases / failure modes:
- `?profile=` blank or whitespace → no profile named.
- case: `Work` → `work` (profile ids are canonicalised lower-case); `default` → the root profile.
- an invalid id (`../x`, `a b`) → 404 without echoing the raw value.
- a tombstoned or half-created (no identity marker) named profile → 404; nothing is created by a
  read (R04.25).
- a single-profile host: a write without a profile acts on the backend's profile (nothing to
  confuse).
- the backend's own profile named explicitly → the backend's scope itself.
- another profile's scope: its own home, `.env`-aware env view built from the process env with
  `INDUS_HOME` pinned to that home, its config and its secret store; marked `routed`.

Our design differences / decisions:
- No process-global "multiplex" flag: "more than one profile" is read from the root at request
  time (`list_profiles`), so a profile created while the backend runs is counted at once.
- Unknown and invalid ids both answer 404 `profile_not_found`.
- No per-request caching of other profiles' scopes (config and `.env` are re-read, so edits made
  by the CLI show at once); a cache can come later if the REST pages need it.

Tests we will write: two temp profiles: write without `profile` → 400; read without → backend
profile; unknown → 404; invalid → 404 (no echo); tombstoned → 404 and no dir created; explicit
other profile → its home/config/secrets; single profile write without → 200; route introspection:
every write route of a sample app depends on `write_scope` (and an offending route is found); a
source scan of `src/indus/web/routes` and `src/indus/web/ws` finds no process-env/home reads.

## Decisions for the lead
- The base env of another profile's scope is the backend's inherited process env (`EnvView.base`)
  with `INDUS_HOME` replaced. If P18-01 publishes the backend profile's `.env` into the process
  env (single-profile CLI mode does), names from it would leak into other profiles' scopes; P18-01
  should not publish `.env` into `os.environ`.

Compare checklist (filled after the rewrite; reopened `web/src/lib/api.ts`,
`hermes_cli/web_routers/_common.py`):
- [x] write without a profile on a multi-profile host → 400; single-profile host keeps the
  backend's profile (test: `tests/unit/web/test_scope.py::test_two_profiles_write_needs_a_profile`,
  `test_scope.py::test_one_profile_host_writes_without_naming_it`)
- [x] reads default to the backend's profile; explicit profile wins
  (test: `test_scope.py::test_another_profile_gets_its_own_home_env_config_and_secrets`)
- [x] unknown → 404, nothing created (test: `test_scope.py::test_unknown_invalid_and_deleted_profiles_are_404`)
- [x] every write route depends on the write dependency (test:
  `test_scope.py::test_every_write_route_depends_on_the_write_scope`)
- [x] route modules read no home/env (test: `test_scope.py::test_route_modules_never_read_the_home_or_the_process_env`)
- Deliberate differences: "more than one profile" is read from the root per request, not decided
  once at boot; no process-global profile swap (a `Scope` object per request instead of a context
  manager that rewrites module paths); an invalid id is 404 like an unknown one.
- Not taken: the SPA prefix list (P18-18 owns it).

Integration (P18 integrator, 2026-10-20): `install(app, ProfileScopes(base))` runs in the app
factory. The `.env` concern above was real for a named-profile launch of the machine dashboard
(the root scope reused the launching profile's env view); `run_launch` now builds the default
profile's own scope (D-16.584). Two fixes from the first REST builder: the error bodies carry
`code` beside `error` (F1's client reads `detail.code`), and `unscoped_write_routes` walks
included routers (FastAPI 0.142 keeps an included router as one wrapper route, so the check had
found nothing in `ROUTERS` routers) — tests `test_included_routers_are_checked_too`,
`test_the_real_app_installs_the_resolver` (D-16.593).

## Status

Done (2026-10-20). Shared: none.
