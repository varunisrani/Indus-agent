Wave 26 (single slice K5) builds from main e23555d. Landed: P00–P15, P21, P22, P16 (17 adapters + kit), P17-01..12
(runtime ledger, admissions, turn lease, liveness, generation fences, event log/replay, request re-delivery, daemon session
host with many viewers, frozen creation policy, single-use tickets + service/ws_auth.py, control socket attach verbs
`session-ticket`/`describe`, shared JSON-RPC dispatcher in service/rpc).
Read in full first: docs/plan/phases/P17-daemon-shared-backend.md (rows 13 and 14, Files, Traps, Acceptance, Run until
green), AGENTS.md, HANDOFF.md, docs/plan/phases/LEAD-NOTES.md "P17 wave 1 landing notes (integrator)" and "P17 wave 2
landing notes (integrator)" — the latter lists exactly what is owed to P17-13 (real ws_url, authenticate_upgrade +
Dispatcher loop, gateway.ready frame with replay_epoch/profile_id/instance_id) and why the daemon cannot yet build its
DaemonSessionHost (P17-22). docs/plan/16-what-we-change.md §2.7 rows D-16.360..435, and the P17 task notes in docs/notes/P17/.

Slice:
- K5 = P17-13 WebSocket endpoint (Starlette WS route in the daemon on 127.0.0.1, ephemeral port recorded in the runtime
  status and returned by `describe`; auth only through service/ws_auth.authenticate_upgrade; message loop =
  decode_request -> Dispatcher -> outgoing events checked with check_event; gateway.ready first frame) + P17-14
  subscriptions and cross-surface events (session.subscribe/unsubscribe/sessions.watch over the host's viewers, replay
  since cursor, re-offer open requests, per-connection authorisation: a connection only sees sessions it attached;
  slow-viewer overflow handling).
  If the daemon cannot host DaemonSessionHost yet, build the endpoint and subscriptions against the host interface
  and serve them from a host the endpoint is given (tests use the real DaemonSessionHost over the fake model); record
  exactly what P17-22 must wire. No stand-ins in src/; test-only doubles only where unavoidable, named as such.
Security: loopback bind only; ticket subprotocol only (never query string); 4401/4403 close codes per ws_auth; origin
allow-list; per-message size cap; params never logged; a slow or hostile viewer cannot stall others or grow memory
without bound; generation fences honoured for interrupt/steer/respond.

Rules:
- P16: each adapter owns only its adapter folder, its wire fake and its tests, plus its own pyproject extra row and
  THIRD_PARTY_NOTICES rows. No P16 task edits a P15 core module: if you need a core change, STOP that part and
  report it to the lead with the exact change proposed. Each adapter implements `create_adapter(AdapterConfig)`,
  a send-only open for `indus send` (no inbound consumption), media fetchers via extras.intake.register_fetcher
  where the platform has media, joins the shared contract suite (tests/contract/gateway conftest parametrisation),
  and passes its Verify column. Check how the registry rows name your platform (src/indus/gateway/registry.py).
- Third-party libraries: optional extras with exact pins (uploaded ≥14 days before exclude-newer 2026-10-04),
  imported lazily, pyright-ignored; the contract suite must run your adapter over its wire fake WITHOUT the SDK
  installed. Licence must be on licenses_allow.json (MIT, BSD, ISC, Apache-2.0, PSF, MPL-2.0 …) — LGPL/GPL/AGPL
  are NOT allowed: if the natural library is copyleft, write our own minimal client over httpx/asyncio stdlib
  instead and record the decision. List every new package (incl. transitives you can see) with its licence.
  Leave uv.lock to the lead (`uv run --frozen` if needed).
- Credentials only from AdapterConfig / SecretStore (vendor env names per D-16.22); never in logs, exception
  text, reprs or argv. No credential-shaped literals anywhere (build fake tokens at runtime).
- No real network (socket guard stays on), never write under the real ~ or /root, no stray processes/sockets.
- Config rows: only your tasks' rows in foundation/config/schema.py and its test (list them as shared).
- D-ids: do not number them; list intended differences under "Decisions for the lead" in each task note.
- Every task: note first in docs/notes/<phase>/<slug>.md; Read the reference paths in your row, close them,
  write; Compare after; tests per Verify; update only your trap/ACC rows. Clean room: never copy reference code,
  comments or docs.
- Work only in your worktree; never commit.
Before finishing: uv run ruff check <paths>, uv run ruff format --check <paths>, uv run pyright <paths>,
uv run lint-imports, uv run pytest <your test dirs> tests/contract/gateway -q -p no:cacheprovider -m "not perf",
uv run python -m tooling.checks --require-ref.
Usage: the lead may message you to wrap up. Then stop starting new tasks, get finished tasks passing, report
done / partial.
Report: files (new / shared), task status, test/check results, wiring for the lead, decisions, licences,
follow-ups.
