Wave 29 builds from main e02d3f5. Landed: P00–P15, P21, P22; P16 31/36 (P16-25, 33–36 building in parallel: do not
touch gateway/adapters, apps/photon-sidecar, cli/doctor/gateway_checks.py); P17-01..22 (P17-23..25 parked in
.agents/wip/w28 — idle exit, profile_guard + protocol error codes/methods; P17-26/27 building in tests/integration/service and
tests/e2e/service: do not touch those). Read the parked w28-l2.patch section in .agents/wip/w28/README.md: the protocol codes
and methods it adds (TICKET_INVALID, PROFILE_MISMATCH, runtime.describe, admission.receipt …) will land before you; where
you need them, stand them in.
Read in full first: your phase file (docs/plan/phases/P18-web-dashboard.md or P19-acp-mcp-serve.md — your rows, Files,
Prework, Traps, Acceptance, Run until green, Depends on), AGENTS.md, HANDOFF.md, docs/plan/phases/LEAD-NOTES.md (all P17
sections, P12/P13/P15 sections your rows rely on), docs/plan/16-what-we-change.md §2.7 rows for your phase, and the code your
rows build on (service/, gateway/ws, service/ws_auth.py, service/rpc, protocol/, packages/client, apps/tui, packages/ui).

Slices (each owns only its tasks' files; stand in other slices' pieces only under tests/.../_stubs_<slice>.py (TS:
src/test/_stubs_<slice>.ts) with FIRST LINE "# STAND-IN: removed at integration" (TS: "// STAND-IN: removed at
integration"), and state the interface you assumed):
- W1 = P18-01..06: web app factory, serve modes, SPA serving; loopback token gate, public allowlist, Host guard, middleware
  order; gated mode (username/password, cookies, audit log); OIDC + service-bearer providers; public URL, trusted proxies,
  path prefix; one WebSocket auth helper over service/ws_auth.py. Security-critical: constant-time compares, no secrets in
  logs/URLs, password hashing only via an allowed library (no hand-written crypto), cookie flags, CSRF per the row.
- W2 = P18-07..10: PTY bridge, PTY keep-alive registry, chat WebSocket routes (/api/pty, events fan-out, RPC sidecar), profile
  scope per request. Stand in W1's app factory / WS auth helper if you need them.
- F1 = P18-17..22 frontend prework (apps/web or the path the phase file names; pnpm workspace; @indus/ui; Biome; vitest):
  SPA shell, REST client, Sessions, Config and Keys, Logs/Analytics/Files, Cron/Skills/Plugins/MCP pages — against fake
  REST/WS servers in tests, exactly as the rows' Prework says. TypeScript only.
- A1 = P19-01..07: `indus acp` entry and stdio purity, initialize/authenticate, backend interface + --local host, attached
  host (daemon viewer over the landed RemoteSessionService/channel), session/new, load/resume/fork, list.
New libraries: only if the row names one; optional extra or pnpm dependency with exact pins (Python: uploaded ≥14 days
before exclude-newer 2026-10-04), licence on licenses_allow.json (no LGPL/GPL/AGPL). Leave uv.lock and pnpm-lock.yaml
changes to the lead: list each new package with its licence (if pnpm needs the lock to run tests, run
`pnpm install --offline` with the store pointed into the scratchpad and report it).

Rules:
- Real servers/daemons in tests bind loopback only, run in a temp INDUS_HOME (and HOME) under tmp_path, and are stopped.
- ALWAYS set INDUS_HOME and HOME to temp dirs before running any `indus` command by hand. Never write under the real ~ or
  /root (point uv/pnpm/npm caches into the scratchpad). Never pkill by a pattern that could hit other worktrees' processes.
- No credential-shaped literals anywhere (build fake tokens at runtime). Tokens/tickets never logged, in argv or in URLs.
- No hand-written cryptography in src/ (use cryptography/allowed libraries; HMAC via stdlib hmac).
- Config rows: only your tasks' rows in foundation/config/schema.py and its test (list them as shared).
- D-ids: do not number them; list intended differences under "Decisions for the lead" in each task note.
- Every task: note first in docs/notes/<phase>/<slug>.md; Read the reference paths in your row, close them, write; Compare
  after; tests per Verify; update only your trap/ACC rows. Clean room: never copy reference code, comments or docs.
- Work only in your worktree; never commit.
Before finishing: uv run ruff check <paths>, uv run ruff format --check <paths>, uv run pyright <paths>, uv run lint-imports,
uv run pytest <your test dirs> -q -p no:cacheprovider -m "not perf", uv run python -m tooling.checks --require-ref; for TS:
CI=true pnpm test --maxWorkers=1, pnpm -r typecheck, biome.
Usage: the lead may message you to wrap up. Then stop starting new tasks, get finished tasks passing, report done / partial.
Report: files (new / shared), task status, test/check results, wiring for the lead, decisions, licences, follow-ups.
