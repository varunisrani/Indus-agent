# Wave 38 parked patches (P24 backend, second half)

## w38-w1.patch: P24-38 cloud browsers + CDP + real profile, P24-39 engines + browser_exec, P24-40 browser_vault, P24-41 hybrid routing — done; P24-47 broker NOT STARTED (base e52fc08)
- Builder checks: ruff, format, pyright 0, lint-imports; unit tools + test_vault + contract cli + config + test_browser_host +
  test_layers 1910 passed / 6 skipped; browser dir 209 passed after the last edit; similarity passed alone after the last edit
  (full tooling.checks before it: all but similarity green) — integrator reruns. No packages (proposed extras browser-cloud =
  playwright 1.63.0, vault = cryptography 50.0.2, both already locked).
- New: tools/builtin/browser/{cloud, real_profile, engines, exec_cli, vault, routing}.py, cli/commands/vault.py, tests unit
  tools/builtin/browser/test_*, cli/commands/test_vault.py; notes browser-cloud, browser-engines, browser-vault, browser-routing,
  browser-broker (P24-47 design only, status not started).
- Shared: browser/session.py (choice modes, Opened cleanups, fallback, sidecar routes, guarded / new_profile / env_for, end/release
  close the sidecar), browser/tools.py (navigate routes + engine/notice), browser/__init__.py (dialog uses manager.current),
  browser/snapshot.py (password / cc-* / one-time-code / vault-filled → [hidden]), tests/fakes/browser.py (eval_hook), schema
  browser.provider, engine, camofox_url, real_profile, real_profile_dir, route_private_local, vault_sources, control_enabled,
  control_developer_mode (control_* unused until P24-47) + test; cli/app.py (vault), contract help catalogue + non_tty_table.
- WIRING OWED: browser_vault + browser_exec not registered by default (golden arrays unchanged): register_vault_tool(registry,
  DEFAULT_TOOLS.manager), register_exec_tool(registry), add both to toolsets.BROWSER_TOOLS, regenerate golden prompts (vault takes
  the `broker` handler extra); add lightpanda + agent-browser to pm/packages.toml (engines.locate_tool asks indus.pm.tool_path
  first, then scope PATH).
- Security: cloud keys from SecretStore then scope env; each cloud session released exactly once on every exit; user CDP attach
  only; real profile opt-in, never copied/deleted, dedicated lock, SingletonLock refused; vault origin exact-match before and
  inside the fill, payment fill needs approval, Fernet local vault key INDUS_VAULT_KEY in SecretStore, 1Password master on stdin,
  Bitwarden master in child-only env, tokens never in argv; filled values join the redaction set (test scans results + log);
  `indus vault add` hidden prompts only, exits 1 without a TTY; browser_exec checks URL policy first, caps step output 4,000
  chars, kills the process group at the deadline.
- Decisions (D-ids): real profile never copied (user's browser must be closed; Chrome 136+ needs a non-default
  real_profile_dir); real-profile failures refuse (reference falls back); routing off by default (reference on); browser_exec
  fixed step list (not model-written Python); local-vault lock is a marker file, agent unlock needs approval, lasts per process.
- Follow-ups: BUILD P24-47 from browser-broker.md (asyncio broker, BROWSER_CONTROLLER ws_auth purpose, API mount); ACC rows
  C22/C24/C25 + trap R02.22 browser drivers; verify server-request event log/redelivery does not record secret-request answers;
  Camofox sub-requests unguarded; Lightpanda Chrome fallback stateless.

## w38-s1.patch: P24-42 egress proxy, P24-43 memory caps, P24-44 plugin packs — done except Compare; P24-45 hub extras PARTIAL (snapshot untested); P24-46 NOT STARTED (base e52fc08)
- Builder checks: ruff, format, pyright 0, lint-imports; last run 776 passed (own + docker + hub + contract cli); earlier wider
  3000 passed / 3 skipped; tooling.checks ok (4 similarity hits in egress.py rewritten). No packages (proposed extra egress =
  cryptography 50.0.2, already locked). Compare step NOT done for P24-42..45 (checklists unticked).
- New: backends/docker/egress.py (asyncio proxy; certs via cryptography; TLS opened only for allow-listed hosts; per-container
  token swapped for the SecretStore secret; others tunnelled; non-public addresses refused; indus-egress posture label;
  enforcement refuses bypass settings; detached daemon + pidfile + health, stop signals only a verified pid),
  backends/memory_scope.py (systemd-run --user --scope MemoryMax from terminal.background_memory_max default 1G clamp
  64M..4G; probe cached 60 s; no user bus → uncapped + debug line; OOM → "killed: memory cap"), cli/commands/{egress,
  plugin_pack}.py, plugins/packs.py (40-hex SHAs; secret/consent/allow_* seeds stripped; user values win; no --yes; one
  review + per-plugin consent; export only git-installed), learning/skills/hub/{sources_extra (ClawHub, LobeHub, browse.sh),
  publish (one commit via the Git data API + PR; refuses escaping symlinks), snapshot (export|import wired, NO TESTS)}.py;
  tests + fakes/systemd_run.py + fixtures/plugins/packs; notes egress-proxy, memory-caps, plugin-packs, hub-extras,
  suggestions-blueprints (P24-46 note only).
- Shared (unions): backends/docker/environment.py (posture mounts / add-host / env names; LABEL_EGRESS in reuse labels;
  open_docker(egress=) — containers made before this have no posture label and are no longer reused; the reaper removes them),
  backends/processes.py (wraps local jobs; memory_cap; OOM mapping), schema EgressSection + terminal.background_memory_max,
  cli/app.py (egress), cli/commands/plugins.py (public ask_env / dependencies_ok; pack group), cli/commands/skills_hub.py
  (publish, snapshot), hub/sources.py (SourceSet + fallback + routing + base-URL overrides), contract help catalogue +
  non_tty_table, tests/fakes/backend/docker_cli.py (--add-host value flag), hub/test_sources.py.
- Decisions (D-ids): off-list hosts tunnelled by default (egress.block_other_hosts = reference default-deny); per-container
  tokens stored as digests; `egress enable` needs --yes without a TTY; NODE_EXTRA_CA_CERTS (no NODE_OPTIONS change); pack
  seeds stripped with a warning; bare `name` pack entries unresolvable (no plugin catalogue); publish pins API 2022-11-28;
  snapshots record content hashes + report drift.
- TODO: snapshot tests; Compare for P24-42..45; build P24-46 (suggested jobs + blueprints, /suggestions + /blueprint slash rows,
  gateway + TUI surfaces); ACC D15-1, D16-1, G7-1, F27-1 + traps R02.20, R07.19, R07.26; verify ClawHub / LobeHub / browse.sh API
  shapes against vendor docs.
