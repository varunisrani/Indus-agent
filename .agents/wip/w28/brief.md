Wave 28 builds from main 3d74df4. Landed: P00–P15, P21, P22; P16 24/36 (an integrator is landing P16-02/03/07/19/21/22/30 in
parallel — do not touch gateway/adapters or apps/whatsapp-bridge); P17-01..22 (runtime ledger, admissions, turn lease,
liveness, generation fences, event log/replay, DaemonSessionHost, tickets + ws_auth, control socket describe/session-ticket,
service/rpc dispatcher, WS endpoint mounted in the daemon, subscriptions, ensure + attach channel + RemoteSessionService,
CLI and TUI attach by default, @indus/client WS transport, store ownership, messaging on the shared host).
Read in full first: docs/plan/phases/P17-daemon-shared-backend.md (your rows, Files, Traps, Acceptance, Run until green),
AGENTS.md, HANDOFF.md, docs/plan/phases/LEAD-NOTES.md (every "P17 …" section, especially "P17-15..22 landing notes
(integrator)" which lists what remains for P17-23..27), docs/plan/16-what-we-change.md §2.7 rows for P17, the task notes
in docs/notes/P17/, and the code your rows build on.

Slices (each owns only its tasks' files; stand in other slices' pieces only under tests/.../_stubs_<slice>.py with FIRST
LINE "# STAND-IN: removed at integration", and state the interface you assumed):
- L1 = P17-23 idle exit for auto-started daemons (gateway/daemon/idle_exit.py; only daemons started by ensure; stop
  reason idle_exit recorded so the next ensure restarts silently; fake clock tests).
- L2 = P17-24 profile binding on attach (profile_id in describe + gateway.ready; clients refuse profile_mismatch before
  sending anything; every daemon RPC under the daemon's Scope; grant helper clears launch-option env vars) + P17-25
  protocol additions and codegen (declare every listed method/event/error code in indus.protocol — several may already
  exist from P17-13..22: reconcile, one meaning per code; regenerate JSON Schema and packages/protocol ONLY with
  `python -m indus.protocol.codegen`; stale check stays green). L2 owns the protocol package and codegen output.
- L3 = P17-26 dual-host test matrix (session_host fixture over inprocess/attached; P06 CLI chat, P12 bridge and P15 Demo 3
  suites under both; vitest hosts.ts for TUI; allow-list file with D-ids — propose ids as "D-?" for the lead) + P17-27
  shared-backend demo E2E (tests/e2e/service/test_shared_backend.py, every step in the row). Tests only: if a real
  behaviour difference or bug shows up, fix it only if small and in P17 code, else STOP that part and report the exact
  change proposed. If L2's profile_id is needed, stand it in.

Rules:
- Real daemons in tests run in a temp INDUS_HOME (and HOME) under pytest tmp_path; stop every daemon/process you start;
  loopback only; no real network (socket guard stays on except where the row's loopback test needs it).
- ALWAYS set INDUS_HOME and HOME to temp dirs before running any `indus` command by hand. Never write under the real ~
  or /root. Never pkill by a pattern that could hit other worktrees' processes.
- No credential-shaped literals anywhere (build fake tokens at runtime). Tickets never logged, in argv or in URLs.
- Config rows: only your tasks' rows in foundation/config/schema.py and its test (list them as shared).
- No new packages. Leave uv.lock alone.
- D-ids: do not number them; list intended differences under "Decisions for the lead" in each task note.
- Every task: note first in docs/notes/P17/<slug>.md; Read the reference paths in your row, close them, write; Compare
  after; tests per Verify; update only your trap/ACC rows. Clean room: never copy reference code, comments or docs.
- Work only in your worktree; never commit.
Before finishing: uv run ruff check <paths>, uv run ruff format --check <paths>, uv run pyright <paths>,
uv run lint-imports, uv run pytest <your test dirs> tests/integration/service -q -p no:cacheprovider -m "not perf",
uv run python -m tooling.checks --require-ref; for TS changes CI=true pnpm test --maxWorkers=1 and pnpm -r typecheck.
Usage: the lead may message you to wrap up. Then stop starting new tasks, get finished tasks passing, report
done / partial.
Report: files (new / shared), task status, test/check results, wiring for the lead, decisions, follow-ups.
