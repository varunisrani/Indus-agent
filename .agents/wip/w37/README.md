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

## w37-l1.patch: P24-15 LSP client + manager, P24-16 edit diagnostics + LSP stop gate, P24-17 verify + verify-on-stop — done
- Builder checks: ruff, format, pyright 0 (incl. tools/core/cli), lint-imports; lsp/verify/stop-gate/config/lint 312 passed /
  2 skipped; wider tools/core/contract cli/foundation/test_layers 3361 passed (non-TTY table rows then added, rerun green);
  unit cli + contract cli 1763 passed; tooling.checks ok (13 similarity spans rewritten; pins check: invalid package.json
  fixture now built in-test). No packages.
- New: src/indus/tools/lsp/{__init__,servers,client,manager,diagnostics}.py, tools/verify/{__init__,recipes,runner}.py,
  core/stop_gates.py (registry, verify-on-stop, pre_verify), core/stop_gates_lsp.py, cli/commands/{lsp,verify}.py,
  tests/fakes/lsp_server.py (scriptable stdio fake), tests/fixtures/verify/workspaces/ (10), tests unit tools/lsp,
  tools/verify, core/test_stop_gates_lsp.py; notes lsp-client.md, lsp-diagnostics.md, verify.md.
- Shared (unions): schema LspSection (lsp.*) + VerifySection (verify.*) + tests; cli/app.py (lsp, verify rows), contract help
  catalogue + non_tty_table (lsp, lsp status, lsp install-hint, lsp restart, verify); tools/builtin/lint.py (P05-20 update: LSP
  tier only when a checker is installed via sys.modules; skips container paths); ACC-C14-2, A28-1, A28-2 (C14-1 Review line
  removed); traps R02.22.
- WIRING OWED (exact steps in lsp-diagnostics.md): per host process LspManager(scope=…) + diagnostics.install_active(
  EditDiagnostics(manager)), manager.trust(<work tree>) per session, await manager.release(path) before removing a worktree,
  await manager.close() at exit; wrap the dispatcher hook seam with LspToolHooks(inner=<plugin seam>) EVEN WHEN LSP IS OFF
  (verify-on-stop reads diagnostics.turn_edits); in core/loop.py::_handle_answer run StopGates.run(StopContext(session_id,
  turn_id, final, tuple(turn_edits(owner, turn)), task_id=…)) before accepting a text answer, on a decision add
  continuation_rows(decision, final), flush, keep final as the budget fallback, continue; register LspStopGate(),
  VerifyOnStopGate(host_for, enabled=lambda: verify.on_stop), PreVerifyGate(plugin_hooks.pre_verify,
  limit=verify.max_hook_nudges); host_for returns the session's terminal environment.
- Decisions (D-ids): only error-severity diagnostics; LSP gate message durable row, verify / pre_verify nudges scaffold rows;
  verify-on-stop runs the workspace build + test phases and returns failing output (≤ 2 per turn) — reference only nudges;
  recipe start commands bind 127.0.0.1; `indus verify` runs commands through the approval rules with nobody to ask (needs-
  approval refused); not built: LS auto-install (hints only), multi-root sharing, lsp.exclude_roots, --save recipe file;
  `indus lsp restart` affects only its own process (daemon verb follow-up); pull diagnostics only from servers advertising
  diagnosticProvider.

## w37-r2.patch: P24-08 relay connector transport, P24-09 relay adapter + ops, P24-10 A2A adapter (both directions) — done
- Builder checks: ruff, format, pyright 0, lint-imports; relay + a2a + delivery + contract gateway + config + gateway
  test_config + unit tools 2054 passed / 2 skipped; config + tools + unit cli 2622 passed / 3 skipped; tooling.checks ok. No
  packages (httpx-ws 0.9.0 already locked — suggested extra `relay = ["httpx-ws==0.9.0"]`; A2A needs no extra: drop the
  phase file's `a2a` extra row).
- New: src/indus/gateway/relay/{__init__ (build_front, relay_settings), descriptor, transport, adapter}.py,
  gateway/adapters/a2a/{__init__ (A2A_ENTRY, create_adapter), adapter, protocol, tools}.py, tests/fakes/platform/relay_wire.py,
  tests/fixtures/a2a/schema/a2a-v1-subset.json (OUR subset — replace with the vendor A2A v1.0.x schema, Apache-2.0), tests unit
  gateway/relay + adapters/a2a; notes relay-transport.md, relay-adapter.md, a2a.md; docs/user/relay-contract.md.
- Shared (unions): gateway/delivery/router.py (attach/detach via_relay=True, native_state= lookup, sender_for relay rule — R1
  also edits this file); tools/toolsets.py (a2a set + indus-a2a bundle); schema gateway.relay.*, a2a.* (a2a.peers.<name>),
  platforms.a2a + tests; ACC-K12-12, ACC-K37-1; traps R06.24.
- WIRING OWED: registry A2A_ENTRY in builtin_entries / BUILTIN_NAMES + NON_CHAT row in test_adapter_matrix.py (→
  tests/unit/gateway/adapters/a2a/test_adapter.py) + setup-wizard step + target-parser rule +
  BuiltinModule("indus.gateway.adapters.a2a.tools") in BUILTIN_MODULES; daemon: build_front(scope, clock) →
  transport.start(); create_relay_adapter per descriptor + router.attach(name, sender, via_relay=True); native_state= to
  DeliveryRouter; RelayFront.on_interrupt stops the chat's turn; idle-exit calls go_dormant(). Relay adapters are not
  registry entries (their contract run is tests/unit/gateway/relay/test_adapter.py).
- Security: signed hello HMAC-SHA256 (stdlib hmac) over gateway id + timestamp + nonce, ±300 s window, replayed nonces refused;
  auth refusal final; A2A JSON-RPC loopback by default, bearer per inbound peer INDUS_A2A_TOKEN_<PEER> (no token → 401 even on
  loopback); inbound text framed untrusted, never runs commands; per-peer 60 calls/min → 429; dm_is_private false; a2a_call
  privacy note + credential masking; private/loopback/link-local URLs refused unless peer trusted, cloud metadata always
  refused; approval cards never shown to A2A peers.
- Decisions (D-ids): our own relay wire contract (not the reference connector's); fresh timestamp + nonce per dial; dormant waits
  for wake() / next start; A2A fail-closed without tokens; input-required from the clarify card; not built: A2A push +
  SubscribeToTask, multi-agent routes, a2a_discover / list / history / orchestrate, relay passthrough plane.
- Follow-ups: vendor schema fixture; 👀→✅ processing reactions via relay `react`; live lanes (owner); stop() fake-clock note.
