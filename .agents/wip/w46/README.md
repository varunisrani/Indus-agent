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

## w46d.patch: RM21 nine desktop data-ui hooks (OWED_HOOKS emptied) + F-2 desktop licences screen (done)
- 22 files, +444/-18. Hooks on Onboarding (dialog + 5 steps), chat/ToolRow.tsx, new files/review PaneToggle in
  panes/PaneShell.tsx; scripts/parity/scenarios.py OWED_HOOKS = {}. Licences: IPC `indus:about:notices` (channels.ts,
  schemas.ts, handler in electron/diagnostics.ts reads <resources>/THIRD_PARTY_NOTICES.md, ≤4 MiB, null in dev),
  global.d.ts regenerated, settings/LicencesPage.tsx at Settings → About → Licences (plain text <pre>).
- Checks: biome lint/fmt, desktop typecheck, check:ipc, ruff, pyright, lint-imports, security_static, tooling.checks ok;
  vitest 204 passed; pytest parity/contract/security 2063 passed, 1 load-flake (test_runner dry run; passes alone).
- W46-d-1..4. MERGE NOTE: [e] also edits apps/desktop (Shell.tsx, main.tsx; d edits PaneShell.tsx, customPages.tsx,
  channels.ts/schemas.ts) — expect nearby-line conflicts only.
- Slip: one `npx vitest` may have made an npm version check over the network.

## w46b.patch (+ w46b-notices.patch): RM8 re-lock (done) — LOCKFILES INCLUDED (uv.lock, pnpm-lock.yaml)
- aiohttp 3.14.3 → 3.14.4 (exclude-newer moved to 2026-10-08 = lock date 2026-10-22 minus 14 days; pins_meta.json
  lock_date 2026-10-22); pynacl 1.5.0 → 1.6.2 via `[tool.uv] override-dependencies` (discord.py 2.7.1 caps <1.6; W46-b-2);
  vite 8.3.2 → 8.3.3 (root package.json + pnpm-lock). No other package moved. Licences Apache-2.0/MIT/ISC (allowed).
- deps_audit snapshot refreshed through the proxy: 877 packages, 0 failing; 15 advisories gone; 4 aiohttp/vite allow entries
  removed. certifi untouched (owner, RM7).
- APPLY w46b-notices.patch TOO (scripts/release/notices_data.json + THIRD_PARTY_NOTICES.md version keys), or notices --check
  and 4 test_notices tests fail. If [g]'s RS7 changes the notices_data.json layout, re-key aiohttp/pynacl by hand.
- W46-b-1: the policy check on a PR reads pins_meta.json from the base branch — we land on main directly, fine.
- Checks: ruff, lint-imports, tooling.checks ok (deps_audit warn: pypdf, katex, sprintf-js); uv lock --check, pnpm
  --frozen-lockfile ok; slack/discord adapter tests 215 passed; nacl AEAD round trip; apps/web vite build + vitest 281;
  tooling/checks + packaging 562; contract + security 1964.
