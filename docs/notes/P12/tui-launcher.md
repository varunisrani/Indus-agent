# Task: `indus --tui` launcher (P12-32) Phase: 12 Owner: wave-9 launcher builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/main_tui_launch.py`
(whole file, 624 lines: `_resolve_use_tui`, `_make_tui_argv`, `_launch_tui`, the cgroup heap
helpers, the exit summary and the active-session file reader). Ours: research `05-tui.md` §3.29
(flag → env table), `04-cli-config.md` env table, LEAD-NOTES (P06, wave 7), wave-9 lead notes
(launcher env), `docs/notes/P06/{entry-startup,click-app}.md`, `docs/notes/P12/bridge-stdio.md`,
`apps/tui/src/entry.tsx`, `packages/client/src/stdioTransport.ts`. Closed before writing.
Evidence: none.

What it does (own words): `indus` (bare or `chat`) decides between the classic prompt and the
full-screen renderer, and when the renderer wins it starts Node on the renderer's entry script,
waits for it, and then tidies up. The choice runs in a fixed order: `--cli` → classic, `--tui` →
renderer, otherwise a run whose stdin or stdout is not a terminal is always classic (a pipe or a
cron job is never taken over by a setting, R04.19), then `INDUS_TUI=1`, then `display.interface:
tui`, else classic. Before Node starts the launcher pins everything the renderer and its bridge
child need into the child's environment: the profile home, the folder the user launched from, the
production `NODE_ENV`, the bridge command, a temp file the renderer writes the live session id
into, and the launch flags as `INDUS_TUI_*` names (old values of those names inherited from the
shell are dropped first so a stale resume id never leaks into a fresh run). Node's old-space heap
is raised unless the user already chose one, but kept under a container memory limit when one is
found. When the renderer ends the launcher mirrors its exit code (a signal death n → 128+n,
Ctrl+C → 130), prints how to resume the session on a normal end or Ctrl+C, deletes the temp file,
decides about a `-w` worktree, and for the renderer's "update me" code (42) runs `indus update`.
When Node, a usable Node version or the built renderer is missing, or on native Windows, it says
why on stderr and the classic prompt runs instead.

Inputs → outputs: merged `GlobalArgs` + `ChatArgs`, the scope (env view, config, home), the TTY
probes, `PATH`, the cgroup memory files, the renderer files on disk → a child process
(`node [--expose-gc] <entry>` or `tsx <entry.tsx>`) with a prepared env and cwd; stderr notices; an
exit code, or "fall back to classic".

Files: `src/indus/cli/tui_launch.py`, `tests/unit/cli/test_tui_launch.py`; shared:
`src/indus/cli/chat.py` (interface choice delegates here; `--tui` runs the launcher instead of
exiting 2), `tests/unit/cli/test_chat_select.py` (two interface tests follow the new behaviour).

Renderer resolution (first hit wins):
1. `--dev`: the checkout's `apps/tui/src/entry.tsx` through the workspace `tsx` binary
   (`apps/tui/node_modules/.bin/tsx`, then the root `node_modules/.bin/tsx`); refused when
   `INDUS_TUI_DIR` is set (a prebuilt dir has no sources) or no `tsx` is installed.
2. `INDUS_TUI_DIR` naming a folder with `dist/entry.js`.
3. The bundle inside the installed package: `indus/_tui_dist/entry.js`.
4. A source checkout's `apps/tui/dist/entry.js`; when any file under `apps/tui/src` or
   `packages/*/src` is newer than it (or `INDUS_TUI_FORCE_BUILD` is truthy), rebuild first with
   `pnpm --filter @indus/tui run build`; a failed rebuild with an old bundle present runs the old
   bundle after one warning line; without any bundle → "not built" and classic.

Edge cases / failure modes:
- not a TTY + config `tui` → classic, silently; `--tui` on a pipe still launches (the renderer
  refuses without a TTY by itself and exits 0).
- inherited `INDUS_TUI_CWD`/`INDUS_LAUNCH_CWD` from an outer renderer → replaced by this run's cwd;
  a deleted cwd falls back to `PWD` when that is a folder, else the home.
- `-c`/`-r` resolved against the store here (same rules as classic: breadcrumb, workspace, title);
  a miss prints the classic selection error and exits with its code before Node starts.
- `-w` with `-c`/`-r` → exit 2 like classic; a failed worktree → exit 1.
- `NODE_OPTIONS` already holding `--max-old-space-size=…` → left alone (never duplicated).
- cgroup v2 `memory.max` = `max`, empty file, garbage, or v1 "unlimited" sentinel → no limit.
- Ctrl+C reaches the renderer (same process group); the launcher ignores SIGINT while it waits so
  the renderer can reset the terminal first; SIGTERM to the launcher is passed on to the renderer.
- `node --version` unparseable or below 22 → refused as "Node too old"; not 24 → one note line.
- temp file: owner-only, never follows a planted symlink; removed on every exit path.
- update (42): `indus update` with this profile's env and none of the `INDUS_TUI_*` launch names;
  its code is returned.

Our design differences / decisions:
- Our names: `INDUS_TUI_DIR`, `INDUS_TUI_FORCE_BUILD`, `INDUS_TUI_ACTIVE_SESSION_FILE`,
  `INDUS_TUI_RESUME`, `INDUS_TUI_CWD`, `INDUS_LAUNCH_CWD`, `INDUS_MODEL`, `INDUS_TUI_PROVIDER`,
  `INDUS_TUI_REASONING`, `INDUS_TUI_TOOLSETS`, `INDUS_TUI_SKILLS`, `INDUS_TUI_QUERY`,
  `INDUS_TUI_MAX_TURNS`, `INDUS_TUI_TOOL_PROGRESS`, and `1`-valued booleans `INDUS_TUI_YOLO`,
  `INDUS_TUI_CHECKPOINTS`, `INDUS_TUI_PASS_SESSION_ID`, `INDUS_TUI_IGNORE_RULES`,
  `INDUS_TUI_IGNORE_USER_CONFIG`, `INDUS_TUI_SAFE_MODE`. All are cleared before being set.
- `NODE_ENV=production` always, `--dev` included (D-04.2 says the renderer always runs in
  production mode); the user's own `NODE_ENV` is overridden.
- Bridge command: a user-set `INDUS_TUI_BRIDGE_COMMAND` wins; else the `indus` script next to the
  running interpreter (the venv's), else `indus` on `PATH`; else not set (the renderer's default).
- Missing Node / renderer → message + classic (the lead's wave-9 ruling), not exit 1.
- Heap: 3/4 of the cgroup limit (whole MiB), capped at 8,192 MB, no floor: under a tiny limit a
  small heap that ends cleanly beats a kill by the container.
- `--reasoning` is exported rather than refused (the renderer may forward it on create).
- No exit-time token summary: the resume hint is two or three lines (command, title).
- Worktree at exit: no interactive keep/discard question (classic has none either); a tree whose
  session holds messages is bound to that session and kept with a one-line notice; a tree with no
  session is removed when that loses nothing, else kept with the reason.
- Not taken: native/inline renderer mode, client-env JSON, shared-session attach, kanban pin,
  skills sync, workspace self-repair through git, package-manager bootstrap of Node.

Tests we will write:
- decision order table incl. non-TTY + config tui → classic, and `--tui` on a non-TTY → TUI.
- env: home, launch cwd, NODE_ENV, bridge command, stale resume cleared, flags exported, stale
  boolean cleared, inherited cwd replaced.
- heap: appended once, user value kept, cgroup 4 GiB → below 4,096, v2 `max` → default, v1
  sentinel → default.
- renderer resolution: env dir, bundle, checkout fresh/stale (rebuild called), `--dev` with and
  without tsx, `--dev` + env dir refused, nothing → None.
- Node missing → hint naming `--cli` and classic; old Node refused.
- run: exit code mirrored, signal → 128+n, KeyboardInterrupt → 130, resume hint on 0/130 (not on
  1), temp file removed, 42 runs update without TUI names; Windows → notice and classic.
- chat integration: `--tui` with a fake Node runs the fake renderer script and returns its code.

## Decisions for the lead
- No `build` script exists in `apps/tui/package.json` yet; the launcher calls
  `pnpm --filter @indus/tui run build` and expects `apps/tui/dist/entry.js` (P12-33's `tui:build`
  should provide that script).
- The wheel bundle path is `indus/_tui_dist/entry.js`. The `pyproject.toml` force-include row is
  NOT added: hatch fails the wheel build when a forced path is missing, and the bundle is not
  built in CI yet. Add `"apps/tui/dist" = "indus/_tui_dist"` once P12-33 builds it before packaging.
- `-v` (verbose tool progress) has no CLI flag yet; only `-Q` → `off` is exported.
- The renderer (P12-27) must read the `INDUS_TUI_*` names above and write
  `{"session_id": "..."}` into `INDUS_TUI_ACTIVE_SESSION_FILE`; the bridge child must honour
  the boolean names it needs (ignore-user-config / safe-mode are not seen by the bridge otherwise).
- `-c`/`--resume latest` uses the classic sources (`cli`, `oneshot`); a `tui` source is not counted
  unless `session_select.CLI_SOURCES` grows it.

Compare checklist (filled after the rewrite; reopened `hermes_cli/main_tui_launch.py`):
- [x] decision order `--cli`, `--tui`, TTY gate, env, config (test:
  `tests/unit/cli/test_tui_launch.py::test_choice_order`, `test_piped_run_with_tui_setting_stays_classic`)
- [x] gap found: `--safe-mode`/`--ignore-user-config` must not let a stored preference pick the
  renderer — fixed in `wants_tui`, pinned by `test_choice_order` (safe/bare rows) and
  `test_explicit_tui_wins_over_safe_mode`
- [x] env assembly: home, cwd pinned over an inherited one, NODE_ENV, bridge, flags, stale resume
  cleared (test: `test_env_is_pinned_and_stale_names_are_cleared`, `test_user_bridge_command_is_kept`)
- [x] heap: appended once, user value kept, cgroup v2/v1/sentinel (tests: `test_heap_*`,
  `test_cgroup_*`)
- [x] gap found: a floor above a tiny container limit would defeat the limit — floor removed,
  pinned by `test_cgroup_without_a_limit`
- [x] renderer order: env dir → bundle → checkout (stale → rebuild); `--dev` + env dir refused
  (tests: `test_renderer_order`, `test_stale_checkout_is_rebuilt`, `test_dev_*`)
- [x] run, Ctrl+C → 130, resume hint on 0/130 only, temp file removed, 42 → update (tests:
  `test_ctrl_c_in_the_launcher_reads_as_130`, `test_resume_hint_after_ctrl_c`,
  `test_no_hint_on_failure_or_empty_session`, `test_session_file_is_removed`,
  `test_exit_42_runs_update_without_tui_names`)
- Deliberate differences: a signal death returns 128+n (the reference passes the negative code
  on); `NODE_ENV` forced to production even with `--dev` (D-04.2); missing Node/renderer falls back
  to classic instead of exiting 1 (lead ruling); `--reasoning` exported, not refused; no
  `--expose-gc` (our renderer never calls the collector); worktree keep/remove decided without a
  question; the hint has no token totals and no "last renderer session" rung (file or resume id).
- Not taken: inline/native mode, client-env JSON, shared-session attach, terminal-config bridge,
  pre-check for a model under safe mode (the bridge reports it), self-repair of a missing app
  folder, package-manager provisioning of Node, separate inference-model/provider names, image and
  hooks flags (we have no such flags), dropping `INDUS_TUI_CWD` for remote terminal backends.

## Status

Done (2026-10-19). Shared files touched: `src/indus/cli/chat.py` (interface choice delegates to
`tui_launch.wants_tui`; `--tui` launches instead of exiting 2; `TUI_PENDING` removed),
`src/indus/cli/app.py` + `src/indus/cli/args.py` (hidden `--dev` global flag, `GlobalArgs.dev`),
`tests/unit/cli/test_chat_select.py` (two interface tests follow the new behaviour).
