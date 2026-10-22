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
