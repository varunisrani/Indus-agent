# Wave 45 parked patches (base 2f8492c)

## w45-s45.patch: SEC-14, SEC-15, SEC-18 fixed (done)
- 24 files, +666/-108. Each fix has a fail-first test (tests/security: lsp tier per profile, room reply cannot forge a speaker,
  gated unsafe request needs Origin/Referer, https cookie only prefixed, hashed script CSP; desktop audit.test.ts seal()).
- SEC-14: service/lsp_host.py `_TIERS` keyed by profile home; tools/lsp/diagnostics.py per-tier checkers. SEC-15:
  core.untrusted.wrap + gateway/rooms/loop.transcript_entry. SEC-18: IpcRegistry.seal sender check; gate.same_site_request
  (Origin, then Referer, else 403; drain bearer decided earlier); cookies.read prefixed only on https; spa.index_csp with the
  bootstrap's sha256 (kept inline on purpose).
- Checks: ruff/format, pyright, lint-imports, security_static, tooling.checks ok; pytest 5759 passed / 57 skipped (unit
  service/lsp/core/rooms/multiplex/web/cli, contract, security, integration core/gateway/web); pnpm lint/fmt/typecheck/ipc;
  vitest 254; web Playwright auth-gates/auth/dashboard 12 passed. Electron E2E not run.
- D-16.970..975 rows added in §2.7 (976–979 unused); §3.1 range row for the integrator.
- MERGE NOTE: P45 also edits service/lsp_host.py and cli/commands/lsp.py (restart); `_active_manager` now takes a scope.
- RULE BREACH: an unintended pnpm auto-install (via `pnpm exec` on hard-linked node_modules) downloaded packages through the
  proxy into $S; caches deleted, lockfile unchanged.

## w45-p45.patch: P24 partial items (done): message_agent in bot chat, integration offers, LSP release + restart
- 37 files, +1399/-41. New service/bot_turns.py (bot chat runs on platform `bots`, gets platform_toolsets.bots or indus-bots;
  daemon chat + API server only), service/integration_offers.py (blueprints `integrations` field; call sites: mcp add,
  POST /api/mcp/servers, gateway setup, PUT messaging platform enabled), service/lsp_control.py (owner-only control verbs
  lsp-restart, lsp-release), lsp_host restart_servers/release_servers via `_manager_for(scope)`, worktrees before_remove.
- Checks: ruff, pyright, lint-imports, security_static, codegen --check, gen_reference fresh (cli.md regenerated),
  tooling.checks ok; pytest 9443 passed / 65 skipped (contract, security, golden, unit service/gateway/cron/cli/backends/
  subagents/lsp/web/state). Integration: 2 failures under load (test_room_two_members passes on rerun;
  test_boot_recovery crash-left-turn failed twice under load, passed 3/3 and 10/10×4 alone) — integrator must confirm on a
  quiet box.
- D-16.980..983 (984–989 unused); B-DAEMON-1 threat-model row lists the new verbs.
- MERGE with S45: point `_manager_for` at S45's per-profile tier (`_TIERS` keyed by profile home); release_folder must
  release in every tier. Update D-16.878 (message_agent + suggestion offers done). w46h docs need the bots/lsp paragraphs.
- Open: classic CLI in-process release on /worktree prune; message_agent in one-shot turns with no daemon.
