# Wave 37 parked patches (P24 backend, first half). Base for every patch: bd38080.

## w37-m1.patch: P24-01 multiplex host lock + serving, P24-02 scope binding + fail-closed secret reads, P24-03 profile routes + delivery identity, P24-04 shared ingress — done
- Builder checks: ruff, format, pyright 0, lint-imports; own dirs + schema test 151 passed; regression unit
  gateway/service/foundation/web + contract gateway/web + integration gateway 4689 passed / 63 skipped / 0 failed;
  tooling.checks ok (one docstring reworded). No packages, no stand-ins.
- New: src/indus/gateway/multiplex/{__init__,host,scope,routes,ingress}.py; tests unit/gateway/multiplex (world, test_host,
  test_scope, test_no_env_reads, test_routes), integration/gateway/multiplex/test_attach_second_run.py (real child owner,
  always stopped), contract/gateway/multiplex/test_shared_ingress.py; notes multiplex-host, multiplex-scope,
  profile-routes, shared-ingress.
- Shared (unions): gateway/control_socket.py (3 verbs in KNOWN_VERBS + OWNER_ONLY_VERBS), service/ws_auth.py
  (TicketBook(served=), serves()), gateway/core/authz.py (Authorizer.for_scope), gateway/core/session_keys.py
  (profile_of_key), gateway/http/api_server.py (ApiServer(front=), profile_app), schema gateway.multiplex_profiles (bool,
  false) + gateway.profile_routes (list) + test_multiplex_rows_validate; traps R06.26, R05.14, R02.8, R02.29, R03.23, R01.16,
  R06.17; ACC-K50-1/2/3.
- WIRING OWED (nothing calls these from `gateway run` yet): when gateway.multiplex_profiles is on — decide_second_run before
  the per-home instance lock; MultiplexHost claim(); identify_fields() into identify; control_verbs() on the ControlServer;
  served=host.served_ticket_ids to the daemon TicketBook; host.aclose() at stop; serve/unserve hooks start/stop the
  profile's adapters via ProfileScopeCache + AdapterTable; per-profile work through ScopeBinder; Authorizer.for_scope;
  router.identify on intake before the session key, router.session_key / delivery_adapter / remember; ApiServer(front=
  SharedIngress) + webhook receiver.app = SharedIngress(receiver.app); mount secondaries' apps; P16 callback kit needs a
  shared-listener mode; flip D-16.17 to "multiplex available, off by default".
- Decisions (D-ids): refused profile exit 78 supervised / 1 at a shell; secondaries don't inherit proxy env (URLs may carry
  credentials); receiving bot in a JSON sidecar <launch home>/gateway/receiving_bots.json (≤ 20,000 rows) because
  Source.model_validate forbids extra keys — move to a transport_profile column at the next schema change; malformed route ids
  skip the route with a problem line; no periodic rescan / config-change rescan / --replace / standalone shim.
