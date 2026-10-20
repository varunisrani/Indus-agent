# Integrate P17-15..22: K9 (P17-22 messaging on the shared host) + K6 (P17-15 ensure, 16 attach channel, 17 remote host) + K7 (P17-18 CLI attach, 21 store ownership) + K8 (P17-19 TUI attach, 20 @indus/client WS transport)

Worktree: /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/verify14 (from main c32b268). Do NOT
commit. The lead commits and lands. Never write under the real ~ or /root — ALWAYS set INDUS_HOME (and HOME) to a temp dir
before running any `indus` command by hand. No network. Never pkill by pattern. Running in parallel: builders E12
(gateway/adapters/matrix crypto, weixin) and E13 (apps/whatsapp-bridge, gateway/adapters/whatsapp) — do not touch them.

Patches (scratchpad/patches/, all base ac389c3): apply in this order with `git apply --3way`: w27-k9.patch, w27-k6.patch,
w27-k7.patch, w27-k8.patch. Read .agents/wip/w27/README.md (K9, K6, K7, K8 sections — each lists the stand-in it used
and the interface it ASSUMED vs what K6 ACTUALLY built) in full first, the task notes in docs/notes/P17/
(messaging-shared-host.md, ensure.md, remote-channel.md, remote-host.md, cli-attach.md, store-ownership.md,
tui-attach.md, client-ws.md), docs/plan/phases/P17-daemon-shared-backend.md rows 15–22 (+ Traps, Acceptance, Run until
green), LEAD-NOTES P17 sections, AGENTS.md, HANDOFF.md.

Steps:
1. Apply in order; resolve as unions (cli/commands/gateway.py lazy rows ensure + grant; non_tty_table; protocol/methods.py
   additions — regenerate TS ONLY with `python -m indus.protocol.codegen`; gateway/ws/methods.py; side_services.py:
   K9's AttachVerbs wiring + K7's MaintenanceVerb registration; control_socket.py verbs; traps/ACC rows).
2. Replace stand-ins with K6's real API and delete them (`grep -rln '^# STAND-IN' src tests` must be empty):
   - K7 (_stubs_k7.py): wire cli/attach.py's backend_factory to service/gateway_ensure.ensure(...) + home_dialer +
     Channel(surface="cli") + RemoteSessionService(channel, Journal(home)).start() / RemoteHost. Map K7's assumed
     extras (create/resume policy=, submit unattended=, pending(), discard_unknown(session, admission, generation),
     failure reasons) onto what K6/K9 provide; add what is genuinely missing in K6's host (small, tested), or record.
   - K8 (_stubs_k8.py): gateway_grant.py's _ensure_from_p17_15 / _from_ensure_error onto K6's ensure + grant_for(home)
     and Ensured.terminal.
3. END-TO-END (the point of this integration): with K9's daemon now hosting sessions and mounting the WS endpoint, add
   integration tests that (a) start a real daemon in a temp INDUS_HOME, (b) run `indus chat -q` attached (no --local)
   against the fake model and get the answer, (c) a second CLI client attaches to the same session and sees the first
   one's turn live, (d) `indus gateway grant --json` returns a usable grant and the TS contract test attaches through it,
   (e) bulk maintenance is refused/served by the daemon while it owns the profile. Keep `--local` behaviour unchanged.
4. Lead decisions to apply: K9's `resolve_unknown(terminal_write=)` — add the small `discard_unknown(..., *,
   terminal_write=None)` parameter to AdmissionService and wire it (it is the clean way to close a lost turn's
   transcript), with a test; if it ripples beyond admissions/recovery, record instead.
5. Security review: tickets never logged/argv/URL (grant prints only inside protocols on stdout); loopback-only dials;
   owner-only verbs (session-ticket, maintenance-run) peer-verified; journal 0600; ensure scrubs launch env; outcome-
   unknown turns never resent anywhere (CLI, TUI, gateway recovery).
6. D-ids: D-16.480 … D-16.499 in docs/plan/16-what-we-change.md §2.7 (+ §3.1 range row) from the four builders'
   "Decisions for the lead" (e.g. ensure in service layer; INDUS_GATEWAY_AUTOSTART; attach:<surface> principal;
   refused flags; --local keeps the file lock; deliver seam; host opens late; terminal verdicts / stdio fallback).
   LEAD-NOTES section "P17-15..22 landing notes (integrator)" incl. what remains for P17-23..27.
7. No conflict markers (grep '^<<<<<<< ' and '^>>>>>>> ').
8. Full gate, all green, no extras installed (`uv sync --locked`):
   uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run lint-imports
   INDUS_STRESS_SECONDS=10 uv run pytest -q -p no:cacheprovider -rf -m "not perf"
   uv run pytest -q -p no:cacheprovider -m perf     # alone, after
   uv run python -m tooling.checks --require-ref
   CI=true pnpm test --maxWorkers=1   (pnpm install --frozen-lockfile --offline first if node_modules is missing)
   plus the repo's TS typecheck. A failure that passes alone must pass alone 3x AND be root-caused; never skip or mark.
9. Credential-literal scan of the diff, no stray processes/sockets/daemons (stop any daemon you started), nothing under
   /root or ~.
10. Usage / restarts: the lead may message you to wrap up; if the container restarts, re-check the tree and re-run killed
    steps. Stage the final tree with `git add -A`.
Report: files changed beyond the patches, stand-in replacement, end-to-end tests added, D-ids, security findings/fixes,
gate counts, follow-ups for P17-23..27.
