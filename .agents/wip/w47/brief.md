# Wave 47 brief: UI bugs found while capturing screenshots + follow-ups (parallel, time-boxed)

Base: a LOCAL snapshot commit `9a155bbb9a` = main `563b077`-line + the waves 45+46 integration as it stood at 07:08 UTC
(not pushed; the integrator is still fixing 7 test failures in other areas). Your worktree is checked out at it. Work only
on your slice; the lead re-bases your patch onto the landed waves 45+46 with `git apply --3way`.
Read first: AGENTS.md, HANDOFF.md (NEXT STEP: "UI bugs seen while capturing screenshots"), the task notes of the area you
change, and the threat model rows that cite it. Clean room. Task notes: a "Wave 47" section in the existing note (Status first).
Each bug fix is FAIL-FIRST: a test that fails on the base and passes with your fix. TIME BOX about 40 minutes.
Decisions: label them `W47-<slice>-<n>` under "Decisions for the lead".

Slices (builder id in brackets):
- [a] Desktop chat: (1) the composer stays on Stop/Steer for minutes after a turn ends although the model is idle
  (intermittent; a window reload clears it) — find the missing/out-of-order terminal event handling in the desktop chat
  store / session subscription; (2) after a window reload, transcript tool rows lose their path/command summary and the
  expanded row loses Arguments — the rebuilt-from-history rows must carry the same summary and args as live rows.
- [b] Desktop file tools resolve relative paths against the daemon's work folder, not the chat's default folder (the
  Terminal pane uses the right one): a desktop chat's session must run its tools in the chat's folder. Find where the
  session's cwd is set when the desktop creates/attaches a chat; prove with a test that a relative write lands in the chat's
  folder.
- [c] TUI attached to the daemon (the default) answers "… is not served here" for complete.slash, model.options and
  session.list, so the slash menu, Ctrl+O model picker and Ctrl+X sessions picker fail. Serve them over the attach channel
  (or route them to the daemon's session service) with the same results as --local; keep owner/profile scope rules.
- [d] UI polish: (5) dashboard Chat tab in the LIGHT theme renders the embedded TUI's prompt/reply text nearly invisible
  (the xterm theme must follow the dashboard theme, or the TUI must pick readable colours on a light background);
  (6) TUI slash-menu row leaves stray characters at the end of a row (redraw/width bug); desktop slash-menu highlighted row
  description has low contrast; session titles truncated too early ("Chat: How do I") — show the full first line up to a
  sensible width with an ellipsis.
- [e] RL26 Windows: native Windows still calls POSIX-only code for background processes (backends/processes.py), the host
  `indus verify` opens (cli/commands/verify.py → open_local), cron scripts and the code-execution launcher (LocalProcess).
  Give each a Windows branch through the Git Bash backend (backends/local_windows.py) or a clear "use WSL2" refusal; tests
  run on Linux with the platform patched; Windows-only behaviour behind platforms("windows").
- [f] Installs: `indus update --finish` refreshes the fetched dashboard pages for the new release (run
  `python -m indus.web.bundle fetch` from the bundles phase; the CLI layer must not import web, D-16.246); install.ps1
  parity with install.sh (pages step + marker `version`); `locate()` treats pages stamped with another release as stale.
- [g] D-16.487: carry a single-query flag on prompt.submit so the daemon's turn itself runs single_query (floors,
  allowlist, smart mode apply the same as --local, and clarify/sudo also follow --local); then retire w46f's client-side
  `one_shot_answer` (keep its tests' intent). Protocol change: run codegen; keep --check green; regenerate reference pages.
- [h] RS3 rooms: a room turn that needs an approval must be answerable by the person (route it to the room's surfaces like a
  normal approval, with the room/member named), and room events (member turns, replies, stop) are pushed to the surfaces
  that watch the room (desktop/TUI/web subscription). Keep SEC-15's framing.

Rules (ENV RULE), for builder <x>:
- `export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/w47<x>` as ONE statement, then a SECOND
  statement: `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/uv-cache-shared npm_config_cache=$S/home/.npm INDUS_HOME=$S/indus`.
  Repeat both in every Bash call. TMPDIR unset. Never run `indus dashboard` by hand. Use your worktree's own .venv
  (`uv sync --locked --offline`).
- pnpm: `pnpm install --frozen-lockfile --offline` with store scratchpad/pnpm-store-p20; if metadata is missing, you may
  hard-link node_modules from scratchpad/verify25 (same lockfile). NEVER `pnpm run`, `pnpm exec` or `npx` before
  node_modules is complete (they can auto-install from the network); prefer `./node_modules/.bin/<tool>`.
- pytest ALWAYS with `--basetemp=/tmp/w47<x>` (SHORT path; delete after each run); targeted dirs plus tests/contract and
  tests/security.
- No new packages; no credential-shaped literals; no real network; never pkill by pattern; never git stash; never commit;
  never write under the real ~, /root or /. Delete build output when done.
Before finishing: ruff check / format --check / pyright on your paths, lint-imports, security_static, targeted pytest,
tooling.checks --require-ref; TS: biome check/format, tsc for the touched package, vitest you touch.
Report: files, each item done / partial / not done, fail-first evidence, check results, decisions, follow-ups.
