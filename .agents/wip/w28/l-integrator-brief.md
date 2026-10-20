# Integrate P17-23..27: L1 (P17-23 idle exit) + L2 (P17-24 profile binding, P17-25 protocol additions/codegen) + L3 (P17-26 dual-host matrix, P17-27 shared-backend demo)

Worktree: /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/verify16 (from main 05bf1ab). Do NOT
commit. The lead commits and lands. Never write under the real ~ or /root (point uv/pnpm/npm caches and logs into the
scratchpad; npm writes debug logs to ~/.npm/_logs unless HOME/npm_config_cache point elsewhere) — ALWAYS set INDUS_HOME
and HOME to temp dirs before running any `indus` or node command by hand. No network (pnpm offline). Never pkill by pattern.
Running in parallel: P16 builders (gateway/adapters, cli/doctor) and P18/P19 builders (src/indus/web, apps/web, src/indus/acp)
— do not touch them.

Patches (scratchpad/patches/, all base 3d74df4): apply in this order with `git apply --3way`: w28-l2.patch, w28-l1.patch,
w28-l3.patch. Read .agents/wip/w28/README.md (L1, L2, L3 sections) in full first, the task notes in docs/notes/P17/
(idle-exit.md, profile-guard.md, protocol-attach.md, dual-host-matrix.md, shared-backend-demo.md),
docs/plan/phases/P17-daemon-shared-backend.md rows 23–27 (+ Traps, Acceptance, Run until green, Done when), LEAD-NOTES P17
sections, AGENTS.md, HANDOFF.md.

Steps:
1. Apply in order; resolve as unions (gateway/daemon/process.py: L1 idle watch + L2 endpoint-in-try; service/remote/host.py:
   L2 OUTCOME_UNKNOWN journal + presence filter and L3 request.settled -> server_request.cancelled; traps/ACC rows).
2. Cross-slice wiring: L3's dual-host matrix and e2e must run against L2's profile binding and new error codes (attached
   host now sees PROFILE_MISMATCH/OUTCOME_UNKNOWN etc.) and L1's idle exit (make sure no matrix/e2e daemon idles out
   mid-test: set gateway.idle_exit_seconds in their temp profiles where needed, and add one e2e/integration check that an
   ensure-started daemon with nothing attached exits idle_exit and the next `indus chat -q` restarts it silently).
3. Lead decisions to apply (small, tested; if one ripples widely, record instead):
   - Emit `admission.state` from DaemonSessionHost on admission transitions (L2 declared it; L3 needs it for unknown
     admissions), and carry `unattended` on prompt.submit with the daemon applying the run-scoped auto-approve so `-z`
     behaves the same on both hosts — then drop the cli.z.approvals allow-list entry if the matrix agrees.
   - prompt.submit must not answer `generation: 0` when idle: wait for the claim or omit the field until known.
   - Keep D-16.365 (no STALE_GENERATION raise from daemon controls) and D-16.490 (no creation policy on the wire) for now.
   - Move `usage_numbers` to one owner (service layer) used by daemon_host and tui_bridge/events.
4. Security review: profile_id fail-closed; foreign_listener check; launch env (INDUS_YOLO_MODE etc.) never reaches the
   daemon; idle exit never fires with a viewer/turn/admission/cron due; tickets never logged/argv/URL.
5. D-ids: D-16.540 … D-16.559 in docs/plan/16-what-we-change.md §2.7 (+ §3.1 range row) from the three builders'
   "Decisions for the lead" + allow-list entries (fill the YAML's "D-?" with real ids). LEAD-NOTES section
   "P17-23..27 landing notes (integrator)" incl. remaining follow-ups (session.resume stored messages, prompts typed on
   another surface, etc.). P17 "Done when" checklist: say which items hold.
6. No conflict markers (grep '^<<<<<<< ' and '^>>>>>>> '); `grep -rln '^# STAND-IN' src tests apps` empty.
7. Full gate, all green, no extras installed (`uv sync --locked`):
   uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run lint-imports
   INDUS_STRESS_SECONDS=10 uv run pytest -q -p no:cacheprovider -rf -m "not perf"
   uv run pytest -q -p no:cacheprovider -m perf     # alone, after
   uv run python -m tooling.checks --require-ref
   CI=true pnpm test --maxWorkers=1   (pnpm install --frozen-lockfile --offline first if node_modules is missing)
   plus `pnpm -r typecheck` and `python -m indus.protocol.codegen --check`. A failure that passes alone must pass alone 3x
   AND be root-caused (L3 saw two load timeouts: gateway start_status_stop, feishu media); never skip or mark.
8. Credential-literal scan of the diff, no stray processes/sockets/daemons (stop by pid, only yours), nothing under /root
   or ~.
9. Usage / restarts: the lead may message you to wrap up (then stage what you have and report where you stopped); if the
   container restarts, re-check the tree and re-run killed steps. Stage the final tree with `git add -A`.
Report: files changed beyond the patches, wiring/decisions applied, D-ids, security findings/fixes, gate counts per step
with rc, Done-when status, follow-ups.
