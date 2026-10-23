# Wave 50 brief: agent-doable follow-ups after all 648 planned tasks (owner: "why stop the work")

Base: main `7eee12d`. Read first: AGENTS.md, HANDOFF.md, docs/plan/01-clean-room-protocol.md, docs/plan/phases/LEAD-NOTES.md
"Wave 48 landing notes (integrator)" (its Follow-ups list) and "Wave 49 landing notes (integrator)", plus the D-ids your slice
names (search docs/plan for them). Clean room: Read → Rewrite → Compare; reference files for behaviour only, never copy code.
Task note first: docs/notes/followups/w50-<slice>.md (Status at the top; Compare checklist kept). Decisions labelled
`W50-<slice>-<n>`. TIME BOX about 60 minutes. Each slice is small: do exactly it, no widening.

Slices:
- [a] Attached `config.set` (D-16.549): a daemon-side `config.set` the attached TUI host uses, so `/yolo`, `/reasoning`,
  `/fast`, `/verbose`, `/busy` work against the daemon the same as locally (same validation, same notices, scope rules as
  the local host). Also the `/status` queued-message count on the daemon. Protocol change → codegen + twins regenerated with
  the repo's tooling (never by hand). Tests: contract + unit for each command in attached mode.
- [b] Daemon `shell.exec` (D-16.1094) for quick `exec` commands from an attached client: runs under the daemon's existing
  approval / sandbox / timeout rules (no new bypass), output streamed or returned per the existing quick-exec contract.
  Protocol change → codegen + twins. Slice [a] also touches the protocol: keep your schema additions in their own blocks so
  the integrator can merge both; never regenerate around a hand edit.
- [c] Windows: `plan()` must not map a trailing `&` / timeout > 600 s to background on a Windows host the way it does on
  POSIX (find the right Windows behaviour from the phase file + notes); and fix the w47
  `test_windows_starter_boots_and_stops_the_tree` race (0.5 s sleep vs first echo) by waiting on a real signal, not a longer
  sleep. Run the changed tests 20x to show the race is gone.
- [d] Termux follow-ups from wave 49: an `apt` install kind in the install-kind detection used by `indus update`, with a
  Termux message telling the user to update via `pkg upgrade indus` (no self-update on that kind); and a `validate` stage in
  scripts/termux/build.sh (checks the built repo layout with repo.py before signing). Tests without docker or network.
- [e] Desktop identity follow-ups from wave 49: the Linux launcher id / `APP_NAME` follow `channel-identity.cjs` for canary;
  Windows electron-updater gets `publisherName` from the build config and keeps update signature verification on (no
  owner values invented: read the configured publisher, placeholder stays marked owner-to-fill). vitest for both.
- [f] Start-up twins (RM25): a generated config-defaults twin (~100 ms) and a shipped-skills front-matter twin (~25–50 ms),
  following the wave-48 twin pattern (`providers/pricing_table.json`, `protocol/field_docs.json`, the `generated_twins`
  guard and `codegen/twins --check`), runtime falls back to the slow path if a twin is missing or stale-checked in tests.
  Report measured import/start-up before vs after (median of 5), but do not change perf budgets.

Rules (ENV RULE), for builder <x>:
- Your worktree: /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/wt-w50<x> (already created, detached
  at main). Work only there.
- `export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/w50<x>` as ONE statement, then a SECOND
  statement: `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/uv-cache-shared npm_config_cache=$S/home/.npm INDUS_HOME=$S/indus`.
  Repeat both in every Bash call (mkdir -p $S/home $S/indus first). TMPDIR unset. Own .venv (`uv sync --locked --offline`).
- node_modules: hard-link from /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/verify26/node_modules
  (`cp -al`; it matches the lockfile — the main checkout's tree is stale, do not use it); per-package node_modules likewise
  from verify26's apps/*/node_modules and packages/*/node_modules if needed. Use ./node_modules/.bin/<tool>; never
  `pnpm run`, `pnpm exec`, `npx` or `pnpm install`.
- No secrets, no credential-shaped literals (build fake tokens at runtime); no network; no docker; no new packages; never
  pkill by pattern; never git stash; never commit; never write under the real ~, /root or /; never run `indus dashboard`.
  `rm` only inside your $S or worktree, with literal paths, never inside `bash -c`. The container may restart; if resumed,
  check what survived and continue.
Before finishing: ruff check/format, pyright, lint-imports for touched Python; codegen/twins `--check`; biome + tsc + vitest for
touched JS/TS; `python -m tooling.checks --require-ref`; pytest on touched dirs plus tests/contract and the dirs your change
reaches, with --basetemp=/tmp/w50<x>. Remove your /tmp/w50<x> afterwards. Report: files, what each test proves, checks run
and results, decisions, owner items, follow-ups.
