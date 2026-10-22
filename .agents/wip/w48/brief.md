# Wave 48 brief: small follow-ups (parallel, time-boxed)

Base: main `179ff84` (waves 45, 46, 47 landed). Read first: AGENTS.md, HANDOFF.md (NEXT STEP + "Minor UI follow-ups"),
LEAD-NOTES "Wave 47 landing notes (integrator)", the task notes of the area you change. Clean room. Task notes: a "Wave 48"
section (Status first) in the existing note of the area. Each fix is FAIL-FIRST. TIME BOX about 40 minutes; if bigger, do the
safe part and record the rest. Decisions: label `W48-<slice>-<n>` (the integrator assigns D-16 numbers).
Screenshots of the current UI (read-only reference): scratchpad/shots/*.png.

Slices (builder id in brackets):
- [a] RS3 room views: desktop GroupRoom.tsx and the TUI and web room views switch from polling groups.log to groups.watch +
  groups.event (unwatch on close; fall back to polling only if watch is refused), and render `approval.requested` as an
  approval card answered with groups.approve (choices offered by the request; refusal/conflict shown). Keep SEC-15 framing.
- [b] TUI in default (attached) mode: the sessions picker rows show title, model and source like --local (session.list
  rows from service/picker_reads must carry them); serve complete.path (the session's backend and cwd), commands.catalog
  and config.get (read-only) on the daemon socket with the same answers as --local (config.set stays unserved).
- [c] Desktop polish: (1) a visible notice when a chat's folder no longer exists (tools fall back silently today —
  W47-b-3); (2) the transcript scrolls a newly opened approval card into view (it can open below an expanded tool row);
  (3) the About › Version log shows a "ResizeObserver loop completed with undelivered notifications" ERROR line — find the
  observer loop and fix it (or classify that benign browser message correctly), and do not print long run paths there
  if they are not useful; (4) localEcho binds the optimistic user row to the turn's own id once turn.started names it, so a
  history reload keeps the row and the reply in one group.
- [d] Dashboard PTY child (web/ws/pty_route.py TuiChild): drop COLORFGBG and TERM_PROGRAM (and any other variable that pins
  the TUI theme) from the child's env so the colour-scheme reports decide; test it. Plus the terminal tool stops offering
  `background=true` on native Windows (schema/description), and check service/console.py's LocalProcess on Windows (Windows
  branch through local_windows or a clear refusal), tests with win32 patched.
- [e] Performance: `oneshot_first_token` (tooling/perf budget 1500 ms) measures ~1.9 s on this box for main too. Profile the
  one-shot path (`python -X importtime`, cProfile) and cut real startup cost (lazy imports on the one-shot path, avoid
  loading unused providers/adapters, avoid repeated config parsing). Do NOT raise or loosen the budget. Report before/after
  medians measured alternately on the same box (9 samples each).
- [f] Dependency advisories: katex 0.16.47 (GHSA-238p-pmpm-9mq7) and sprintf-js 1.1.3 (GHSA-hp3w-g68c-fv3c) — bump to fixed
  versions within the 14-day release-age rule via pnpm overrides or the parent package, through the proxy only for
  `pnpm install` / the audit refresh; refresh tooling/checks deps_audit snapshot; list every lockfile change with licences.
  If a fix is too new for the rule, leave it and record. Also: the vite 8.3.3 node_modules gap — your install brings
  node_modules in line with the lockfile; run the JS gate (pnpm lint / fmt / -r typecheck / test) once installed.

Rules (ENV RULE), for builder <x>:
- `export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/w48<x>` as ONE statement, then a SECOND
  statement: `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/uv-cache-shared npm_config_cache=$S/home/.npm INDUS_HOME=$S/indus`.
  Repeat both in every Bash call. TMPDIR unset. Never run `indus dashboard` by hand. Own .venv (`uv sync --locked --offline`).
- node_modules: hard-link from /home/user/indus-agent/node_modules (`cp -al`, same lockfile apart from [f]'s change) for
  every builder except [f]; never `pnpm run`, `pnpm exec` or `npx` (they can auto-install); use ./node_modules/.bin/<tool>.
  Only [f] may run `pnpm install` (through the proxy).
- pytest ALWAYS with `--basetemp=/tmp/w48<x>` (short; delete after each run); targeted dirs plus tests/contract and tests/security.
- No new packages (except [f]'s bumps); no credential-shaped literals; no real network (except [f] via the proxy); never pkill
  by pattern; never git stash; never commit; never write under the real ~, /root or /. Delete build output when done.
- The container may restart; if resumed after one, check what survived and continue.
Before finishing: ruff check / format --check / pyright on your paths, lint-imports, security_static, targeted pytest,
tooling.checks --require-ref; TS: biome check/format, tsc for the touched package, vitest you touch.
Report: files, each item done / partial / not done, fail-first evidence, check results, decisions, follow-ups.
