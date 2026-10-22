# Wave 48 parked patches (base 1b6e711). Integrator assigns D-16 numbers to W48-<slice>-<n> decisions.

## w48d.patch: dashboard PTY child env, terminal tool schema on native Windows, admin console on Windows (done)
- 8 files, +346/-19. web/ws/pty_route.py drops HOST_TERMINAL_NAMES (COLORFGBG, TERM_PROGRAM(_VERSION), KITTY_WINDOW_ID,
  WT_SESSION, ZED_TERM, ITERM_SESSION_ID, VTE_VERSION, TMUX(_PANE), ZELLIJ, STY) and pins TERM/COLORTERM;
  tools/builtin/terminal.py schema_overrides → WINDOWS_HOST_SCHEMA (no background/pty/notify/heartbeat/persist);
  service/console.py SubprocessRunner uses start_windows_process (job object) on win32.
- Fail-first: pty env, terminal schema, 3 console tests. pytest 5189 passed. ruff/pyright/lint-imports/gen_reference/
  tooling.checks ok (deps_audit warns: katex, sprintf-js — slice f; NEW pypdf 6.19.0 moderate advisories, fixed 6.20.x).
- W48-d-1..3. Follow-ups: plan() still turns `&` / timeout > 600 s into background on a Windows host; wave-47
  test_windows_starter_boots_and_stops_the_tree raced once in 4 (0.5 s sleep vs first echo); real Windows lane.

## w48a.patch: RS3 desktop room view on groups.watch + approval card (done; TUI/web have no room view)
- 10 files, +652/-28. bots/GroupRoom.tsx follows groups.watch/groups.event (re-watch on gateway.ready from last seq,
  unwatch on close, poll only if watch refused, reset on room switch); new bots/RoomApprovalCard.tsx (offered choices only,
  SEC-15 framing: command in <pre>, description in <blockquote>; armed explicit press; groups.approve; refusal shown);
  canonicalChat RpcLike.onEvent; managementSlots rpcOf stable per connection (WeakMap); BotsPage keys GroupRoom by room id.
- Fail-first: 4 live-room cases, rpcOf identity, room switch. Desktop vitest 732; tsc/biome ok; pytest rooms + contract +
  security + docs 2173. Not seen in a real Electron window (jsdom only).
- W48-a-1, W48-a-2 (TUI/web room views would be NEW surfaces; web needs groups.* on the dashboard socket — owner/lead
  decides if owed for 1.0). RS3 can move to Done for the desktop.

## w48e.patch: one-shot startup cuts (partial: budget still missed on this loaded box)
- 8 files, +235/-9. protocol/base.py ProtocolModel defer_build=True (wire models build on first use, ~390→260 ms import);
  core/subagents/output_schema.py lazy jsonschema (~50 ms); foundation/secrets.py start_backend_discovery() overlapping
  keyring backend discovery with CLI imports (cli/entry.py), W48-e-3: discovery also runs for secret_store: file profiles.
- Before/after (same box, alternating, 9 samples, load 7.5–8.7): wall to first byte 2532 → 2281 ms; perf test median 1917 ms
  vs 1500 ms budget (budget unchanged). Fail-first: 5 new tests. pytest 3238 + 68 + 54; codegen --check unchanged.
- Follow-ups (largest first): pricing YAML ~56 ms (JSON twin), use_attribute_docstrings ~100 ms, httpx._main ~80 ms
  (third party), lazy cli.runtime imports (docker, browser, code kernel, cron.models, content_scan), measure on a quiet runner.
- NOTE: builder [g] continues [e] in scratchpad wt-w48g on a LOCAL base 34c34d1 (= 1b6e711 + w48e.patch); its patch is
  relative to that base, so apply w48e.patch THEN w48g.patch.

## w48f.patch: katex + sprintf-js advisories cleared, node_modules in line with the lockfile (done; pypdf too new)
- 6 files: pnpm-workspace.yaml overrides `mermaid>katex: 0.19.0` (W48-f-1) and `"@electron/get>global-agent": 4.1.3`
  (W48-f-2; sprintf-js has no fixed release), pnpm-lock.yaml (9 packages removed incl. katex 0.16.47 / sprintf-js / roarr;
  global-agent 4.1.3, matcher 4.0.0, serialize-error 8.1.0, type-fest 0.20.2 — all MIT/BSD/ISC/CC0, all older than 14 days),
  deps_audit snapshot refreshed via proxy (868 packages, 0 failing; pypdf warnings only), THIRD_PARTY_NOTICES.md +
  notices_data.json regenerated (433). uv.lock unchanged.
- pypdf (W48-f-3): fixed 6.20.1 becomes eligible at lock_date 2026-10-27 (all 4 moderates); re-lock then.
- Checks: tooling.checks ok; ruff; lint-imports; notices fresh; pytest 2508; pnpm lint / fmt / -r typecheck clean; root
  vitest 2912 passed / 2 load timeouts in apps/web (App, FilesPage; pass alone and at --maxWorkers=1).
- LEAD: scratchpad/wt-w48f is KEPT — its node_modules match the new lockfile (vite 8.3.3). The integrator hard-links them.

## w48c.patch: desktop polish — folder-gone notice, card scroll, ResizeObserver error, localEcho binding (done)
- 19 files, +539/-13. backends/task_overrides.py workdir_gone + core/tool_round.py one `notice` (warning, key
  workdir_missing, loop_texts.WORKDIR_GONE_NOTICE) per turn; ApprovalCard/ClarifyCard useRevealOnOpen scrollIntoView once;
  TerminalPane refit on next animation frame; app/errorForwarding.ts drops the benign ResizeObserver message, trims file:
  URLs (shortenFileUrls); busyActions.namedTurn + localEcho re-binds the optimistic row when turn.started names the turn.
- Fail-first for all four. pytest 3602; ruff/pyright/lint-imports/tooling.checks ok; biome/tsc ok; desktop vitest green
  apart from one-off load flakes in untouched electron/updates/updater.test.ts and electron/backend/resolver.test.ts.
- W48-c-1..5. MERGE NOTE: core/tool_round.py and backends/task_overrides.py were also touched by waves 46/47 (landed);
  w48e/g touch startup only.
