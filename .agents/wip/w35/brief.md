Wave 35 (P20-25..39) builds on a LOCAL base commit c3891d6 = main 454e486 + `.agents/wip/w34/w34-int-wip.patch` (P20-08..24:
backend ladder, installer runner, local attach + tickets, remote, boot orchestrator, supervision, REST proxy, renderer
shell, boot overlay, connection store, transcript, composer, journals, queue, cards, model picker + status bar, slash).
That base is being gated and landed by the lead in parallel; do not depend on its commit id. Landed before it: P00–P19,
P21, P22; P20-01..07 (desktop workspace, Electron main skeleton, preload bridge + 76-channel IPC table, security defaults).
Read in full first: docs/plan/phases/P20-desktop-demo4.md (your rows, Files, Traps, Acceptance, Run until green, Done
when), AGENTS.md, HANDOFF.md, docs/plan/phases/LEAD-NOTES.md (every "P20 …" section) and
`.agents/wip/w34/landing-p20a.md`, docs/notes/P20/*.md (esp. preload-bridge.md: the IPC table with the owner task per row
— implement only your rows' channels; renderer-shell.md: the slot API), docs/plan/16-what-we-change.md §2.7 P20 rows, and
the code under apps/desktop and packages/*.

Slices (each owns only its tasks' files; stand in other slices' pieces only under a _stubs_<slice>.ts / .py file whose
FIRST LINE is "// STAND-IN: removed at integration" (Python: "# STAND-IN: removed at integration"); state the interface
you assumed):
- D1 = P20-25 sidebar (sessions + projects), P20-26 profile rail, routing and switch shapes, P20-27 pane shell and
  interface modes.
- D2 = P20-28 preview pane, P20-29 files pane + fs IPC, P20-30 git review pane, P20-31 terminal pane (mount into D1's pane
  shell through a stand-in registry if needed).
- D3 = P20-32 settings overlay, P20-33 provider onboarding, P20-34 deep links + media scheme.
- D4 = P20-35 HUD, quick entry and extra windows, P20-36 tray, keep awake, notifications, P20-37 updates (unsigned 1.0
  scope).
- D5 = P20-38 packaging (unsigned builds on three OSes: config + scripts + CI job definitions; no real signing, no
  network uploads), P20-39 diagnostics and small surfaces.
P20-40 (Electron E2E + Demo 4) is done at integration.

Rules:
- ENV RULE: set `export S=<your scratch dir>` as ONE statement, then `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache
  npm_config_cache=$S/home/.npm TMPDIR=$S/tmp ELECTRON_SKIP_BINARY_DOWNLOAD=1` as a SECOND statement (one combined
  statement leaves $S empty and writes at `/`). Never write under the real ~, /root or /. Use the pnpm store dir
  /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/pnpm-store-p20 (`pnpm install --frozen-lockfile
  --store-dir <that>`). There is no Electron binary in this container: Electron-launching tests must skip cleanly without
  it; renderer/main logic is tested with vitest + fakes.
- New npm packages only if your row needs one: exact pins, published >= 14 days ago, licence on licenses_allow.json (no
  LGPL/GPL/AGPL); `pnpm install` may change pnpm-lock.yaml — list each new package with licence + publish date in your
  report. No new Python packages.
- Security (Electron): contextIsolation on, nodeIntegration off, sandboxed renderers, every IPC channel validated with its
  zod schema in main, no credential across the bridge except single-use local tickets, fs IPC confined to allowed roots
  (no path traversal, no symlink escape), deep links/media scheme never open arbitrary files or URLs, no shell: true.
- No credential-shaped literals anywhere; no real network; never pkill by pattern; stop every process you start.
- D-ids: do not number them; list intended differences under "Decisions for the lead" in each task note.
- Every task: note first in docs/notes/P20/<slug>.md; Read the reference paths in your row, close them, write; Compare after;
  tests per Verify; update only your trap/ACC rows. Clean room: never copy reference code, comments or docs.
- Work only in your worktree; never commit.
Before finishing: pnpm lint / biome, pnpm -r typecheck, CI=true pnpm test --maxWorkers=1 (or the desktop package's vitest),
the desktop check-ipc + gen-bridge-types --check scripts if you touch the bridge, uv run python -m tooling.checks
--require-ref; for any Python change also ruff, pyright, lint-imports and the relevant pytest.
Usage: the lead may message you to wrap up. Then stop starting new tasks, get finished tasks passing, report done / partial.
Report: files (new / shared), task status, test/check results, wiring for the lead, decisions, packages + licences,
follow-ups.
