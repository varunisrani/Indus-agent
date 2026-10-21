# Integrate P20-25..39 (w35 D1..D5) and build P20-40 (Electron E2E + Demo 4) — completes P20

Worktree: /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/verify18 (from main e12c7b6, which
already contains P20-01..24). Do NOT commit; the lead commits and lands. ENV RULE: `export S=/tmp/claude-0/-home-user/
3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/int35` as ONE statement, then `export HOME=$S/home
XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=$S/home/.cache/uv npm_config_cache=$S/home/.npm ELECTRON_CACHE=$S/electron-cache`
as a SECOND statement. Do NOT point TMPDIR at the long scratchpad path (AF_UNIX path limit); leave it unset. Never write
under the real ~, /root or /. Never pkill by pattern. Builders P23 (wave 36) run in parallel in other worktrees: do not touch
scripts/install, docker/, nix/, flake.nix, src/indus/cli/commands/update*, scripts/release.

Patches (all base c3891d6 = the tree of main 2a1991b; apply on your main with `git apply --3way`): .agents/wip/w35/
w35-d1.patch, w35-d2.patch, w35-d3.patch, w35-d4.patch, w35-d5.patch in that order, then w35-d5-main-ts-wiring.patch.
Read .agents/wip/w35/README.md (every section) in full first, docs/notes/P20/*.md for these tasks,
docs/plan/phases/P20-desktop-demo4.md rows 25–40 (+ Traps, Acceptance, Run until green, Done when), LEAD-NOTES "P20 wave 34
landing notes (integrator)", AGENTS.md, HANDOFF.md.

Steps:
1. Apply in order; resolve unions by reading both sides: electron/main.ts (D1 installProfiles + BackendPort, D2 panes +
   guest preload + paneElectron(), D3 prefs/media/deep links + installBackend return captured, D4 updateGate + ambient +
   updates, D5 diagnostics/context menus/linux launcher) — one coherent install order before registry.seal(); ipc
   channels/schemas + regenerate src/global.d.ts with gen-bridge-types; App.tsx / main.tsx / en.ts / styles.css; package.json;
   pnpm-workspace.yaml; pnpm-lock.yaml (re-lock with `pnpm install` using the store
   /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/pnpm-store-p20, then `--frozen-lockfile` must pass).
2. Cross-slice wiring from the README: D2's panes onto D1's paneRegistry (extend D1's render ctx with workspace, mode,
   connection, openPreview, addToChat; call registerWorkingPanes at startup; delete D2's _stubs_d2.ts); D3 settings hooks
   into D1 (switcher.applyConnectionChange / renameProfile / deleteProfile, ProjectStore), D4 (shortcuts, ambient,
   updates), D5 (<LogsViewer/> in About/Advanced), D2 (fs.getLimits/setLimits); D1 sidebar "pop out" → hud.open; D2 preview
   close via D3's onCloseRequested; D5 packaging writes the build stamp D4 reads and ships electron-updater + node-pty
   (asarUnpack, spawn-helper +x); preload-bridge.md channel table + count.
3. Licences: the workspace must not ship BlueOak-1.0.0 or WTFPL. D4 overrode sax -> 1.4.1 (ISC); D5's electron-builder tree
   (build-only) has BlueOak minimatch / tar / minipass / chownr / yallist / sax / isexe and WTFPL truncate-utf8-bytes. Owner
   ruling pending: keep the prod licence guard green, record the build-only set in THIRD_PARTY_NOTICES / a D-id, and make sure
   none of them is in the packaged app (check-asar / staged build/app).
4. P20-40 Electron E2E + Demo 4: get Electron 44.5.1 for linux-x64 into $ELECTRON_CACHE (curl the release zip from
   github.com/electron/electron/releases via the proxy; electron's install.js fails behind the proxy — unzip into
   node_modules/electron/dist and write path.txt), run under xvfb-run. Build the row's E2E suite + Demo 4 core suite (fake
   backend AND a real daemon in a temp INDUS_HOME): boot, connect, prompt, streamed reply, approval card, sidebar/session
   switch, profile switch, panes (preview with hostile guest pages: window.open, untrusted clicks, file: links refused;
   files confined; terminal env scrubbed; git review), settings search + deep link, HUD + quick entry, tray with/without a
   StatusNotifier host where testable, update check against the loopback fake feed, diagnostics copy redacted, packaged
   `--dir` smoke (D5) if electron-builder can run offline. Record what cannot run here (signed installs, macOS/Windows).
5. Security review: every IPC channel validated in main; fs/media confined (before + after symlink resolution); preview
   guest isolation; deep links never install without confirmation and never add command MCP servers; terminal env floor;
   diagnostics redaction; no credential across the bridge except single-use tickets; link previews refuse private hosts.
6. D-ids: D-16.700 … D-16.739 in docs/plan/16-what-we-change.md §2.7 (+ §3.1 range row) from the five builders' "Decisions for
   the lead". LEAD-NOTES section "P20 wave 35 landing notes (integrator)" incl. P20 "Done when" status and follow-ups.
7. No conflict markers; no STAND-IN files (`grep -rln '^// STAND-IN\|^# STAND-IN' src tests apps packages`).
8. Full gate, all green: uv sync --locked; ruff check + format --check; pyright; lint-imports; codegen --check;
   `INDUS_STRESS_SECONDS=10 uv run pytest -q -p no:cacheprovider -rf -m "not perf"` (Python changed in D1/D3: sessions_store
   lineage_root, models test-endpoint route); perf alone; tooling.checks --require-ref; pnpm lint, fmt:check, -r typecheck,
   check-ipc, gen-bridge-types --check, dump-slash-catalog --check; CI=true pnpm test --maxWorkers=1; the Electron E2E lane.
   A failure that passes alone must pass alone 3x AND be root-caused; never skip or mark.
9. Credential-literal scan of the diff; no stray processes (Electron, xvfb, daemons — stop by pid, only yours).
10. Stage the final tree with `git add -A`. If the lead asks you to wrap up: stage what you have and report where you stopped.
Report: files changed beyond the patches, wiring done, licence handling, P20-40 suite contents + results, D-ids, security
findings/fixes, gate counts per step with rc, P20 Done-when, follow-ups.
