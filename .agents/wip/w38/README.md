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
