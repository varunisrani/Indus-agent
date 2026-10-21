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

## w37-x1.patch: P24-18 `indus import-agent` + P24-23 `indus pm` lock, store, ensure + P24-24 `indus pm` commands — done
- Builder checks: ruff, format (repo clean), pyright 0, lint-imports; unit pm + unit cli + contract cli + foundation config
  2082 passed / 3 skipped (final narrower rerun 813 passed / 2 skipped); tooling.checks ok. No packages (httpx + stdlib).
- New: src/indus/cli/importers/{__init__,agents}.py, cli/commands/import_agent.py, fixtures import_agent/{claude_code_home,
  codex_home,README.md}, test_import_agent.py (33); src/indus/pm/{lock,store,fetch,core}.py + packages.toml,
  pm/commands.py, cli/commands/pm.py, tests/fakes/pm_origin.py, tests/unit/pm/*, tests/unit/cli/commands/test_pm.py; notes
  import-agent.md, pm-core.md, pm-commands.md.
- Shared (unions): src/indus/pm/__init__.py (lazy facade ensure / tool_path / env_for / manager), cli/app.py (import-agent + pm
  rows at the end of REGISTRY), contract help catalogue + non_tty_table (+4 with_arguments rows), schema PmSection
  (pm.mirror_url "", pm.timeout_s 60) + test, pyproject ruff banned-api for indus.pm.{fetch,store,lock} + per-file TID251
  ignores, traps R02.22, R02.4, R04.19 (no ACC-I46 / S9 rows exist).
- Wiring: consumers call indus.pm.ensure(scope, name) → Runner; runner.env(scope, child_env) with
  indus.safety.child_env.child_env (pm cannot import safety: sibling layer); tool_path() never installs (P24-39 uses it);
  no lock ships: `indus pm lock --yes` writes <root>/pm/lock.json (network); a packaged indus/pm/lock.json is picked up
  automatically — the P23 release pipeline could build it.
- Decisions (D-ids): import-agent puts home CLAUDE.md / AGENTS.md into a marked per-agent block in PERSONA.md (seeded with
  DEFAULT_IDENTITY when missing); project instruction files reported, never copied; project files only with --project DIR;
  committed .claude/settings.json allow rules NOT imported (deny rules are), settings.local.json allow + deny imported; project
  MCP servers imported disabled; MCP env values become ${NAME}; credential headers dropped + reported by name; servers whose
  URL/args look like a credential refused; Bash(x:*) → `x` and `x *`; --sync re-applies changed sources only; non-TTY without
  --yes exits 1 after the preview. pm: hash mismatch at the primary falls back to the mirror (reference stops); store entries
  <name>-<version>-<sha[:16]>; declined defaults in facts.json, receipt <root>/pm/receipt.json; `pm env` prints package exports
  only; lock/update need --yes without a TTY. OWNER NOTE: ffmpeg comes from a third-party static build (GPL binary, only run as
  a separate process) — owner may prefer another origin.
- Follow-ups: multi-archive targets, --target staging, progress, .previous-* restore points, sealed-install lazy policy,
  release-built lock, packages.toml versions unverified offline; import-agent doesn't read ~/.claude/projects memories.
