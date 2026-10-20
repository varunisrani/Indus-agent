Wave 27 builds from main ac389c3. Landed: P00–P15, P21, P22; P16-01, 04–06, 08–18, 20, 23, 24, 26–29 (22 adapters on the
P16-01 kit with registry rows, PlatformEntry.lock_env, BUILTIN_PUBLIC_ENV, Adapter.set_drain_probe, _jws.py, _identity.py);
P17-01..14 (runtime ledger, admissions, turn lease, liveness, generation fences, event log/replay, request re-delivery,
DaemonSessionHost with many viewers, frozen creation policy, single-use tickets + service/ws_auth.py, control socket
`session-ticket`/`describe`, shared JSON-RPC dispatcher in service/rpc, WebSocket endpoint gateway/ws (built, not yet
mounted) and service/subscriptions.py).
Read in full first: your phase file (docs/plan/phases/P17-daemon-shared-backend.md or P16-more-platforms.md — your rows,
Files, Traps, Acceptance, Run until green), AGENTS.md, HANDOFF.md, docs/plan/phases/LEAD-NOTES.md (P17: "P17 wave 1
landing notes (integrator)", "P17 wave 2 landing notes (integrator)", "P17-13/14 landing notes (lead)"; P16: every "P16 …
landing notes" section), docs/plan/16-what-we-change.md §2.7 rows for your phase, the task notes in docs/notes/P17/ or
docs/notes/P16/, and the code your rows build on.

Slices (each owns only its tasks' files; stand in other slices' pieces only under tests/.../_stubs_<slice>.py with FIRST
LINE "# STAND-IN: removed at integration", and state the interface you assumed):
- K6 = P17-15 ensure and discovery (`indus gateway ensure`), P17-16 Python attach channel (WS JSON-RPC client over the
  ticket subprotocol), P17-17 RemoteSessionService (SessionService over the channel; journal). Client side of the
  shared backend. Use the landed server pieces (gateway/ws, service/rpc, service/ws_auth, describe/session-ticket)
  as the far end in tests, over an in-process ASGI WebSocket (see tests/fakes/asgi_ws.py) — no real sockets unless
  the row's integration test needs loopback.
- K7 = P17-18 CLI attaches by default (`indus`, `chat`, `-q`, `-z` use ensure + RemoteSessionService; `--local` keeps
  in-process) and P17-21 store ownership across processes (maintenance gate). K6 builds ensure/channel/remote host in
  parallel: stand them in per the rule above.
- K8 = P17-19 TUI attaches by default (`indus gateway grant --json` + apps/tui/src/transport/attach.ts) and P17-20
  `@indus/client` WS transport (packages/client: ticket subprotocol, gateway.ready, replay cursor). TypeScript +
  a small Python helper; run the repo's pnpm/vitest/tsc checks for the TS side; regenerate protocol types only with
  the codegen tool.
- K9 = P17-22 messaging on the shared host: gateway admission submits through the admission service; build the
  DaemonSessionHost inside the daemon (see LEAD-NOTES "P17 wave 2 landing notes" for why it was not possible before
  and what is needed: one host owning the service, broker_factory/turn_door, owner epoch / instance_id source, aclose
  order); mount the WS endpoint (docs/notes/P17/ws-endpoint.md wiring list) and AttachVerbs(host=..., ws_url=...).
  This is the riskiest slice: keep every existing gateway/Demo 3 test green.
- E11 = P16-31 per-platform setup helpers and P16-32 cron and send target parsers for every landed platform (the
  builders' notes list each platform's target shapes: Matrix room ids with ':', Teams ids with ':' and ';messageid=',
  QQ group:/channel:/dm:, Signal group.<id>, email addresses, ntfy topics, IRC targets, Feishu oc_/ou_/cmt_, etc.).
  This touches many adapter folders but only to add small parser/setup modules the rows name; do not change adapter
  behaviour. P16-31 secrets go to the SecretStore, never .env, never echoed.
- E12 = P16-07 Matrix end-to-end encryption (plugs into the `EndToEnd` seam the plain Matrix adapter left; use an
  allowed library — check licences: e.g. vodozemac bindings / matrix-nio / mautrix crypto — in an optional extra,
  imported lazily; the device store lives under the profile home with 0600 files; `send_only` must not start E2EE
  sync) + P16-19 Weixin adapter (iLink Bot; any media crypto via `cryptography`).
- E13 = P16-02 WhatsApp Node bridge sidecar (apps/whatsapp-bridge, pnpm workspace; licence-check the WhatsApp Web
  library — copyleft is not allowed; loopback-only local API with a per-install shared secret; auth state 0600 under the
  profile home) + P16-03 WhatsApp bridge adapter (Python side, uses _identity's JID/LID helpers and mask_number).
  Run the repo's pnpm/vitest/tsc checks for the TS side.
No hand-written cryptography anywhere in src/ (use `cryptography`/allowed libraries; HMAC via stdlib `hmac`).
Never pkill by a pattern that could hit other worktrees' processes.

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
