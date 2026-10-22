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

## w46a.patch: RM2 / SEC-4 fence laundering fixed by taint (done)
- 16 files, +490/-63. core/untrusted.py Taint + fence(tainted=) + fence_as; tools/calls.py TaintSink protocol (layers kept);
  core/turn_state.py, finalize.py, tool_round.py; tools/builtin/code (script's untrusted RPC calls mark the run; sticky per
  kernel owner until reset); core/subagents batch/child/delegation/background (child that read outside text → fenced summary,
  background deliveries fenced, action=stop too). Threat model SEC-4 → fixed (B-INJECT-1), security-review.md "Wave 46".
- Fail-first tests in tests/security, unit core tool_round, code kernel, subagents background. pytest: security 75, code 92,
  subagents 52, core 946, integration/core 28, contract 1891 — all passed. ruff/pyright/lint-imports/security_static/
  tooling.checks ok.
- W46-a-1..4 (whole result fenced; sticky per owner; short/error untrusted runs count; lead files still say SEC-4 open —
  integrator updates HANDOFF/LEAD-NOTES/phase table). Residuals: file write then read_file; model-copied text.
- MERGE NOTE: S45 also edits core/untrusted.py (adds wrap(); _fence_text calls it) and threat-model.md (other rows).

## w46f.patch: RS4 TUI model picker, RS6 lost turn with saved reply, F-5 chat -q approvals (done)
- 15 files, +473/-16. RS4: protocol ProviderOption.models (codegen regenerated: packages/protocol types.ts + schema.json),
  tui_bridge/methods_config.py lists the current custom/local provider via model_list.list_models; TUI rowOf reads it.
  RS6: gateway/daemon/recovery.py discards a routeless lost turn quietly when the transcript proves the reply is saved
  (RecoveryReport.saved_turns). F-5: cli/attach.py `one_shot_answer` — attached finite `chat -q` denies approvals with the
  --local wording (approve once under approvals.single_query_mode: approve); clarify/sudo/secret still exit 3.
- Fail-first tests (tui_bridge listing, integration shared-host routeless lost turn, cli attach). pytest 4018 passed, 1
  flake: test_room_two_members (flakes on main too: 2/4 with main's files) — integrator triages. TUI vitest 42, tsc ok.
- W46-f-1..3; follow-up D-16.487 daemon-side single-query flag. Move F-5 row in rehearsal-log to Done at integration.

## w46g.patch: RS19 post_tool_call for blocked calls, RS7 notices JSON layout, RS9 shellcheck job, RS11 short socket dirs (done)
- 21 files, +355/-27. RS19: tools/dispatch.py run_after on block (no transform), BLOCKED in tools/hooks.py, plugin payload
  `blocked`. RS7: scripts/release/notices.py dump_json matches Biome (160 width cases byte-identical; committed file
  unchanged). RS9: ci.yml new `shell` job (apt shellcheck, version must equal 0.9.0; built install.sh + scripts/release/*.sh)
  and `gate` needs it — first CI run unverified. RS11: tests/short_paths.py + `short_tmp` fixture; watchdogs, rpc, control
  socket, attach verbs, runner line-wrap test fixed.
- Fail-first tests; pytest 7813 passed, 1 load flake (ssh deadline contract; passes alone). gen_reference/notices fresh.
- W46-g-1..3. MERGE NOTE: ci.yml also edited by w44 (windows-local job); the committed notices_data.json layout is
  unchanged by RS7, so w46b-notices.patch should still apply.

## w46k.patch: F-3 prebuilt dashboard pages for script installs, F-4 `indus --version` on a source install (done)
- 11 files, +1018/-21. web/bundle.py `fetch` (HEAD's release tag → tag record manifest_sha256 → manifest.json →
  indus-web-<version>.tar.gz digest-checked, tarfile filter="data", staged swap into src/indus/web/static with a
  .indus-release stamp; https only, size caps, 60 s); web/command.py clear message when no pages and no pnpm;
  cli/version.py reads the release for HEAD from tags / installer marker / update receipts (file reads only);
  install fragment 20-runtime.sh `ins_web_pages` + marker `version` (install.sh rebuilt).
- Fail-first test for F-4; pytest unit web/cli + smoke + contract + security 3854 passed; integration/install 128 passed.
  ruff/pyright/lint-imports/security_static/build.py --check/tooling.checks ok. shellcheck not available.
- RELEASE.YML MUST ADD (integrator, after w44 lands): before `publish.py upload`, `pnpm install --frozen-lockfile`,
  `pnpm --filter @indus/web build`, `tar -C apps/web/dist -czf <upload-dir>/indus-web-${VERSION}.tar.gz .` (VERSION without
  v); scan that dist with the artefact/reference-host scan; rehearsal row "script install without Node serves the pages".
- W46-k-1..5. Follow-ups: `indus update --finish` refresh of fetched pages; install.ps1 parity; stale-stamp handling.
