# Wave 39 brief (lead: Agent 2) — P24 UI wave (desktop + dashboard)

Main is `f16f343`: P00–P22 complete (P20 desktop app complete in `52deb8b`), P23 being landed in parallel (scripts/install,
docker/, nix/, src/indus/cli/{update,uninstall}.py, release workflows — do not touch those), P24 backend slices parked in
`.agents/wip/w37/` and `.agents/wip/w38/` (multiplex, rooms/bots/peers, relay + A2A, board/kanban/projects, LSP + verify,
import-agent + `indus pm`, browser providers, egress proxy, memory caps, plugin packs, hub sources) — NOT on main yet: read
their READMEs for the seams/interfaces they add; where your UI needs one of their routes or RPCs, code against the interface
the README states and stand it in (rule below).

Read in full first: docs/plan/phases/P24-multiplex-extras.md (your rows, Files, Fixtures, Traps, Acceptance, Run until green,
Done when, "Depends on"), AGENTS.md, HANDOFF.md, docs/plan/phases/LEAD-NOTES.md (P18 and P20 landing sections),
docs/plan/16-what-we-change.md §2.7 rows for P24 and P20, docs/notes/P20/*.md (preload-bridge.md: the IPC channel table —
each new channel is one row in apps/desktop/electron/ipc/channels.ts + schemas.ts with an owner task, then
`node apps/desktop/scripts/gen-bridge-types.mjs`; every handler goes through the registry with sender checks), the P18 web
notes for dashboard rows, and the code your rows build on (apps/desktop, apps/web, src/indus/web, src/indus/service).

## Slices (each owns only its rows' "New files")
- U1 = P24-19 desktop pet + `/pet`, P24-20 wake word, P24-25 preview annotate mode + agent-driven preview automation,
  P24-26 capabilities page, P24-33 themes and locale plumbing.
- U2 = P24-27 management pages part 1 (messaging, webhooks, scheduled jobs, profiles), P24-28 part 2 (agents, command
  center, learning map, artifacts, session import), P24-29 connections registry + remote auth, P24-30 SSH remotes with
  managed updates, P24-31 bots in the desktop app.
- U3 = P24-32 desktop plugin SDK + contribution registry, P24-34 uninstall from Settings (calls the landed/landing
  `indus uninstall` — read src/indus/cli/uninstall.py from .agents/wip/w36/p23-integration-green.patch; never deletes without
  the row's confirmation flow), P24-35 desktop voice, P24-36 dashboard admin console, P24-37 dashboard plugins.
Stand-ins: other slices' or parked backend pieces only under tests/.../_stubs_<slice>.py (Python) or
src/test/_stubs_<slice>.ts (TS) with FIRST LINE "# STAND-IN: removed at integration" / "// STAND-IN: removed at
integration"; state the interface you assumed in your note.

## Rules
- ENV RULE: `export S=<your scratch dir>` as ONE statement, then `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache
  UV_CACHE_DIR=$S/home/.cache/uv npm_config_cache=$S/home/.npm npm_config_store_dir=$S/home/.pnpm-store
  ELECTRON_CACHE=/tmp/claude-0/-home-user-Indus-agent/c19ae23d-3736-57d9-a6ce-64f23794b389/scratchpad/electron-cache` as a
  SECOND statement, then `echo $HOME` (must be under the scratchpad), BEFORE your first uv/pnpm command. Leave TMPDIR unset
  (long paths break Unix sockets). Never write under the real ~, /root or /. Electron's install.js fails behind the proxy:
  unzip $ELECTRON_CACHE/electron-v44.5.1-linux-x64.zip into node_modules/electron/dist and write path.txt.
- New libraries only if the row names one: exact pins, published >= 14 days before 2026-10-20 (npm) / exclude-newer
  2026-10-04 (Python), licence on tooling/checks/licenses_allow.json; leave lock files to the lead (you may install in your
  worktree to test) and list each package with licence and date.
- Security: IPC sender checks via the registry only; no credential crosses the bridge except single-use tickets;
  credentials never in URLs, argv, logs or exception text; plugin code (U3) runs with the row's isolation and never gets
  the bridge's privileged channels; wake word / voice (U1/U3) never record without the row's consent and indicator.
- No credential-shaped literals (build fake tokens at runtime). Never pkill by pattern; stop every process you start.
- Task note first: docs/notes/P24/<slug>.md. Reference /root/reference/hermes-agent (pinned ab3fe9d) only in Read and
  Compare; own words/structure; similarity hit → restructure. D-ids: do not number; "Decisions for the lead" in each note.
- Work only in your worktree; never commit.

## Before finishing
TS: pnpm lint, pnpm fmt:check, pnpm -r typecheck, node apps/desktop/scripts/check-ipc.mjs, gen-bridge-types --check,
(cd apps/desktop && CI=true pnpm test --maxWorkers=1), (cd apps/web && CI=true pnpm test --maxWorkers=1) as touched.
Python touched: ruff check/format --check, pyright, lint-imports, targeted pytest -q -p no:cacheprovider -m "not perf".
Always: uv run python -m tooling.checks --require-ref. Do NOT run the full suite.
If the lead messages you to wrap up: stop starting new tasks, get finished tasks passing, report done / partial.

## Report (short)
Files (new / shared), task status per id, IPC table changes, stand-ins + interfaces, checks with exact counts, wiring for
the lead, decisions, packages + licences, follow-ups, any environment slip.
