# Wave 46 parked patches (base 8a7735e). Integrator assigns D-16 numbers to W46-<slice>-<n> decisions.

## w46e.patch: RS16 desktop contribution areas + plugins inventory pane (done)
- 10 files, +778/-2 (apps/desktop/src/contrib/areas.tsx + test, Shell.tsx, main.tsx, themeStore.ts, AppearancePage.tsx,
  strings, styles.css; note docs/notes/P24/desktop-plugin-sdk.md "Wave 46").
- title-bar strip, minimal CommandPalette (Ctrl/Cmd+Shift+P) with palette-command entries, contributed keybinds (app keys
  reserved), plugin themes (#rrggbb tokens only) in Appearance, `app.plugins` pane renders PluginsPage.
- Checks: biome check/format, desktop tsc, check:ipc, security_static, tooling.checks --require-ref ok; desktop vitest 1572
  passed. pnpm install fell back to --prefer-offline (may have fetched metadata through the proxy; no lockfile change).
- Decisions W46-e-1..4 (title strip, minimal palette, keybind collisions vs default keys only, hex-only theme tokens).
- Follow-ups: composer-extension, session-row-decoration, model-menu-decoration, appearance-setting, transcript-directive
  consumers; plugin sockets; respect personal rebinds in keybind conflicts.

## w46h.patch: RS10 user-guide pages for wired P24 features (done)
- 13 files, +637: docs/user/user-guide/{board,bots-and-peers,rooms,themes,pets,vault,egress,helper-programs,lsp,verify}.md,
  docs/user/messaging/relay.md, mkdocs.yml nav, docs/notes/P25/docs-guide.md "Wave 46". Docs only.
- Checks: mkdocs build --strict ok; tests/unit/docs + test_nix_files 106 passed; gen_reference --check fresh; tooling.checks
  ok; security_static ok; ruff clean.
- W46-h-1: once P45 lands, bots-and-peers.md (message_agent in bot chat) and lsp.md (`indus lsp restart` vs a running daemon)
  each need a paragraph — the w45+w46 integrator adds them. W46-h-2: no runnable doc examples (need daemon/network).
